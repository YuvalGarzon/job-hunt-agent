"""Store one secret in your system's keychain (service "job-hunt-agent") with hidden input.

Usage (in a terminal, from the project folder, with .venv active):
    python scripts/set_secret.py TELEGRAM_BOT_TOKEN

The value is read with hidden input and never printed or written to disk.
For multi-line values (a service-account JSON), paste it and finish with an empty line.
"""
import getpass
import sys

import keyring

SERVICE = "job-hunt-agent"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: set_secret.py NAME")
    name = sys.argv[1]
    multiline = name.endswith("_JSON")
    if multiline:
        print(f"Paste the value for {name}, then press Enter on an empty line (input is hidden):")
        lines = []
        while True:
            line = getpass.getpass("")
            if not line.strip():
                break
            lines.append(line)
        value = "\n".join(lines)
    else:
        value = getpass.getpass(f"Paste the value for {name} (input is hidden): ")
    value = value.strip()
    if not value:
        sys.exit("empty value, nothing saved")
    keyring.set_password(SERVICE, name, value)
    print(f"saved {name} in the system keychain ({len(value)} characters)")


if __name__ == "__main__":
    main()
