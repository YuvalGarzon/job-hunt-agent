"""All job sources. fetch_all() runs every company board in parallel with timeouts;
fill_details() adds the descriptions that big-company listings leave out."""
import concurrent.futures as cf

import yaml

from agent import config
from agent.sources import ats, bigco

FETCHERS = {**ats.FETCHERS, **bigco.FETCHERS}
ONE = {**ats.ONE, **bigco.ONE}


def _data() -> dict:
    """companies.yaml: the boards fetched every run (setup.py starts it from the example)."""
    if not config.COMPANIES_PATH.exists():
        return {"companies": []}
    return yaml.safe_load(config.COMPANIES_PATH.read_text(encoding="utf-8")) or {"companies": []}


def companies() -> list:
    return list(_data().get("companies") or [])


def company_tiers() -> dict:
    data = _data()
    tiers = {n.lower(): 1 for n in data.get("tier1_names", [])}
    tiers.update({n.lower(): 2 for n in data.get("tier2_names", [])})
    for c in data.get("companies") or []:
        tiers.setdefault(c["name"].lower(), c.get("tier", 3))
    # the user's favorite companies (settings.yaml search.favorite_companies) score as tier 1
    tiers.update({n.lower(): 1 for n in config.search().get("favorite_companies") or []})
    return tiers


def fetch_all(max_workers: int = 16, deadline: float = 35, extra: list = None) -> tuple:
    """Returns (jobs, errors). A failing board is reported, never fatal. `extra` adds boards in
    companies.yaml form (e.g. found through LinkedIn alerts)."""
    jobs, errors = [], {}
    comps = companies()
    names = {c["name"].lower() for c in comps}
    comps += [c for c in extra or [] if c["name"].lower() not in names]
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(FETCHERS[c["ats"]], c): c for c in comps}
        try:
            for f in cf.as_completed(futs, timeout=deadline):
                c = futs[f]
                try:
                    jobs.extend(f.result())
                except Exception as e:
                    errors[c["name"]] = f"{e.__class__.__name__}: {str(e)[:120]}"
        except cf.TimeoutError:
            for f, c in futs.items():
                if not f.done():
                    errors[c["name"]] = "timed out"
    return jobs, errors


def fetch_one(source_ref: str) -> dict:
    """Fetch one posting again (title, description, url), or None if it is gone."""
    kind, rest = source_ref.split(":", 1)
    if kind == "web":                                   # web:page:<url> (a careers page the user sent)
        from agent.intake import resolve_page
        job = resolve_page(rest.split(":", 1)[1])
        return job and {"title": job.title, "description": job.description, "url": job.url}
    board, jid = rest.rsplit(":", 1)
    return ONE[kind](board, jid)


def fill_details(jobs: list, deadline: float = 15, max_workers: int = 8) -> set:
    """Fetch the description of each job listed without one, up to a per-source cap and a
    time limit. Returns the job_ids that now have their details; the rest wait for the next run."""
    picked, per_source = [], {}
    for j in jobs:
        n = per_source.get(j.source, 0)
        if n < bigco.DETAIL_CAP.get(j.source, bigco.DETAIL_CAP_DEFAULT):
            per_source[j.source] = n + 1
            picked.append(j)
    done = set()
    ex = cf.ThreadPoolExecutor(max_workers=max_workers)
    futs = {ex.submit(bigco.DETAIL[j.source], j): j for j in picked}
    try:
        for f in cf.as_completed(futs, timeout=deadline):
            try:
                if f.result():
                    done.add(futs[f].job_id)
            except Exception:
                pass                                   # checked again next run
    except cf.TimeoutError:
        pass
    ex.shutdown(wait=False, cancel_futures=True)
    return done
