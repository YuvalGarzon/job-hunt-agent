"""Make the Sheet readable: one visible tab, "My Applications".

It lists only the jobs you applied to (Applications rows with a Date applied), newest first,
with a color per status and the overdue follow-ups in red. It is a formula over the
Applications tab, so it updates by itself; the code never writes it.

All the agent's own tabs (Applications, Jobs, Events, Queue, Inbox, Health, Settings, Geo)
stay as they are, only hidden: the agent needs them. Unhide one from the "All sheets" list
(the ≡ button at the bottom left) to look inside.

Safe to run again at any time.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.sheet import TABS, Sheet, _retry, col_letter  # noqa: E402

VIEW = "My Applications"
NAVY = {"red": 0.12, "green": 0.22, "blue": 0.39}
WHITE = {"red": 1, "green": 1, "blue": 1}

# (header, Applications column, width px); the sheet is sorted by "Applied on"
COLS = [("Company", "Company", 150), ("Role", "Role", 300), ("Status", "Status", 110),
        ("Applied on", "Date applied", 100), ("Follow up on", "Follow-up due", 105), ("Next step", "Next step", 170),
        ("Fit", "Fit score", 55), ("Location", "Location", 110), ("Job link", "Job link", 260),
        ("Last update", "Last update", 135), ("Notes", "Notes", 360)]
SORT_BY = "Applied on"

ROW_COLORS = {   # status -> row background
    "Applied": {"red": 0.87, "green": 0.92, "blue": 0.99},
    "Assessment": {"red": 1.0, "green": 0.93, "blue": 0.80},
    "Interview": {"red": 0.84, "green": 0.94, "blue": 0.84},
    "Offer": {"red": 0.66, "green": 0.88, "blue": 0.66},
    "Rejected": {"red": 0.97, "green": 0.85, "blue": 0.85},
    "Withdrawn": {"red": 0.93, "green": 0.93, "blue": 0.93},
    "No response": {"red": 0.93, "green": 0.93, "blue": 0.93},
}
STATUS_TEXT = {  # status -> bold colored text in the Status cell
    "Applied": {"red": 0.10, "green": 0.33, "blue": 0.75}, "Assessment": {"red": 0.80, "green": 0.45, "blue": 0.0},
    "Interview": {"red": 0.10, "green": 0.50, "blue": 0.15}, "Offer": {"red": 0.0, "green": 0.40, "blue": 0.0},
    "Rejected": {"red": 0.75, "green": 0.10, "blue": 0.10}, "Withdrawn": {"red": 0.45, "green": 0.45, "blue": 0.45},
    "No response": {"red": 0.45, "green": 0.45, "blue": 0.45},
}
RED = {"red": 0.80, "green": 0.0, "blue": 0.0}


def _formula() -> str:
    letter = {h: col_letter(i + 1) for i, h in enumerate(TABS["Applications"])}
    cols = ",".join(f"SUBSTITUTE(LEFT(Applications!{letter[c]}2:{letter[c]},16),\"T\",\" \")" if c == "Last update"
                    else f"Applications!{letter[c]}2:{letter[c]}" for _, c, _ in COLS)
    applied = letter["Date applied"]
    sort_col = [h for h, _, _ in COLS].index(SORT_BY) + 1
    return (f'=IFERROR(SORT(FILTER({{{cols}}}, Applications!{applied}2:{applied}<>""), {sort_col}, FALSE),'
            f' "No applications yet")')


def main() -> None:
    s = Sheet()
    fields = {"fields": "sheets(properties,conditionalFormats)"}
    meta = _retry(s.sh.fetch_sheet_metadata, fields)
    sheets = {m["properties"]["title"]: m for m in meta["sheets"]}
    if VIEW not in sheets:
        _retry(s.sh.add_worksheet, title=VIEW, rows=500, cols=len(COLS))
        sheets = {m["properties"]["title"]: m for m in _retry(s.sh.fetch_sheet_metadata, fields)["sheets"]}
    _retry(s.sh.worksheet(VIEW).update, [[h for h, _, _ in COLS], [_formula()]], "A1",
           value_input_option="USER_ENTERED")

    v = sheets[VIEW]
    sid = v["properties"]["sheetId"]
    n = len(COLS)
    names = [h for h, _, _ in COLS]
    st = col_letter(names.index("Status") + 1)
    fu = col_letter(names.index("Follow up on") + 1)
    body = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": n}
    reqs = [{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}} for _ in v.get("conditionalFormats", [])]
    reqs += [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "index": 0, "hidden": False,
                                                  "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2},
                                                  "tabColor": {"red": 0.20, "green": 0.66, "blue": 0.33}},
                                   "fields": "index,hidden,gridProperties.frozenRowCount,gridProperties.frozenColumnCount,tabColor"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": n},
                        "cell": {"userEnteredFormat": {"backgroundColor": NAVY, "horizontalAlignment": "CENTER",
                                                       "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP",
                                                       "textFormat": {"foregroundColor": WHITE, "bold": True, "fontSize": 11}}},
                        "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": body,
                        "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP",
                                                       "textFormat": {"fontSize": 11}}},
                        "fields": "userEnteredFormat"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 36}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 1, "endIndex": 500},
                                       "properties": {"pixelSize": 28}, "fields": "pixelSize"}},
        {"updateBorders": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 500, "startColumnIndex": 0, "endColumnIndex": n},
                           "innerHorizontal": {"style": "SOLID", "color": {"red": 0.85, "green": 0.85, "blue": 0.85}}}},
    ]
    for i, (h, _, w) in enumerate(COLS):
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                                                   "properties": {"pixelSize": w}, "fields": "pixelSize"}})
        if h in ("Status", "Applied on", "Follow up on", "Fit", "Last update"):
            reqs.append({"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": i, "endColumnIndex": i + 1},
                                        "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                                        "fields": "userEnteredFormat.horizontalAlignment"}})
        if h == "Company":
            reqs.append({"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": i, "endColumnIndex": i + 1},
                                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 11}}},
                                        "fields": "userEnteredFormat.textFormat"}})

    def rule(formula, fmt, rng):
        reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [rng], "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                                             "format": fmt}}}})

    st_i = names.index("Status")
    status_cell = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": st_i, "endColumnIndex": st_i + 1}
    fu_i = names.index("Follow up on")
    fu_cell = {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": fu_i, "endColumnIndex": fu_i + 1}
    for status, color in ROW_COLORS.items():
        rule(f'=${st}2="{status}"', {"backgroundColor": color}, body)
    for status, color in STATUS_TEXT.items():
        rule(f'=${st}2="{status}"', {"textFormat": {"foregroundColor": color, "bold": True}}, status_cell)
    # follow-up date reached and still waiting for an answer -> red
    rule(f'=AND(${fu}2<>"", ${st}2="Applied", ${fu}2<=TEXT(TODAY(),"yyyy-mm-dd"))',
         {"textFormat": {"foregroundColor": RED, "bold": True}}, fu_cell)

    # every other tab hidden (kept for the agent)
    for title, m in sheets.items():
        if title != VIEW:
            reqs.append({"updateSheetProperties": {"properties": {"sheetId": m["properties"]["sheetId"], "hidden": True},
                                                   "fields": "hidden"}})
    _retry(s.sh.batch_update, {"requests": reqs})
    print(f"'{VIEW}' ready; hidden: {', '.join(t for t in sheets if t != VIEW)}")


if __name__ == "__main__":
    main()
