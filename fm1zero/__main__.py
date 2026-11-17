"""fm1zero from a shell (over SSH is the easy way to prove the USB side before trusting the screen app).

    python3 -m fm1zero              the app, on the CardputerZero's screen
    python3 -m fm1zero status       what is plugged in. Sends nothing but the identity question
    python3 -m fm1zero info         the FM-1's chip. Restarts the FM-1, writes nothing
    python3 -m fm1zero backup       save its whole flash to backups/
    python3 -m fm1zero install FILE.fwsc
    python3 -m fm1zero restore FILE.bin
    python3 -m fm1zero refresh      read The Firmware Hub's list (once a day)
    python3 -m fm1zero get ID       download a firmware from its maker, by its id in the list
"""

from __future__ import annotations

import sys

from . import catalogue, device, jobs, pkg as pkgmod
from .store import Store


def _say(text: str) -> None:
    print("..", text, flush=True)


def _report(p: jobs.Progress) -> None:
    if p.total:
        print("\r   %3d%%  %d written " % (100 * p.done // p.total, p.written), end="", flush=True)
        if p.done == p.total:
            print()


def _sure(question: str) -> bool:
    return input(question + "\nType yes to go ahead: ").strip().lower() == "yes"


def _chip(result: device.Result) -> str:
    c = result.chip
    from . import boot

    return ("bootloader: %s\n  %s\n" % (result.bootloader_text, boot.RECOVERY[result.bootloader]) +
            "firmware before: %s\nfirmware after:  %s\nflash: %s (%06X), %d KiB, reads of %d\nchip key: %04X  id: %s\n"
            "boot ROM: %s  loader: %s  %d MHz\nwritable by this tool: %s" % (
                result.before or "?", result.after or "?", c.flash_name, c.flash_id, c.size // 1024, c.io, c.key,
                c.uid or "?", c.rom or "?", c.loader or "?", c.mhz, "yes" if c.supported else "NO"))


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "ui":
        from .ui import main as ui_main

        return ui_main()
    cmd, args = argv[0], argv[1:]
    store = Store()
    try:
        if cmd == "status":
            from . import usb

            for port in usb.midi_ports():
                print("midi: %s  %04x:%04x  %s%s" % (port.path, port.vid, port.pid, port.name,
                                                    "" if port.candidate else "  (not asked: not an FM-1 name)"))
            state, detail = device.status()
            print({"rom": "FM-1 in its boot loader (4c4a:8057)", "firmware": "FM-1 running: " + detail,
                   "none": "no FM-1 that answers"}[state])
        elif cmd == "info":
            print(_chip(device.identify(_say)))
        elif cmd == "backup":
            result = device.backup(_report, _say)
            saved = store.add_backup(result.data, {"identity": result.before, "uid": result.chip.uid,
                                                   "flash": "%06X" % result.chip.flash_id,
                                                   "sha256": jobs.sha256(result.data)})
            store.note("Backup saved as %s" % saved.name, history=True)
            print("saved", saved.path)
        elif cmd == "install" and args:
            package = pkgmod.load(args[0])
            print("%s: for %s, flash image %d bytes, sha256 %s" % (args[0], package.product, package.flash_len,
                                                                 package.sha256))
            cache = catalogue.read_cache(store.catalogue_path) or {}
            chip_rule = catalogue.chip_rule(cache.get("entries", []), package.sha256)
            if chip_rule is None:
                print("This file isn't in the Hub's list, so whether it suits the newer flash chip isn't known.")
            if not _sure("Install it on the FM-1? No backup is made."):
                return 1
            result = device.install(package, chip_rule, _report, _say)
            store.note("Installed %s: %d sectors written. Now: %s" % (args[0], result.written, result.after or "?"),
                       history=True)
            print("done: %d sectors written and read back. It now says it is: %s" % (result.written,
                                                                                  result.after or "(no answer)"))
        elif cmd == "restore" and args:
            with open(args[0], "rb") as f:
                image = f.read()
            if not _sure("Restore %s onto the FM-1? Only for the unit it came from." % args[0]):
                return 1
            result = device.restore(image, _report, _say)
            store.note("Restored %s: %d sectors written. Now: %s" % (args[0], result.written, result.after or "?"),
                       history=True)
            print("done: %d sectors written and read back. It now says it is: %s" % (result.written,
                                                                                  result.after or "(no answer)"))
        elif cmd == "refresh":
            cache, message = catalogue.refresh(store.catalogue_path)
            print(message)
            print(catalogue.ATTRIBUTION)
        elif cmd == "get" and args:
            cache = catalogue.read_cache(store.catalogue_path) or {}
            entry = next((e for e in cache.get("entries", []) if e["id"] == args[0]), None)
            if not entry:
                print("No firmware with that id in the list (run refresh first).")
                return 1
            raw = catalogue.download(entry)
            print("saved", store.add_package(entry, raw).path)
        else:
            print(__doc__)
            return 2
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
    except Exception as e:
        print("\nFAILED:", e)
        if getattr(e, "wrote", False):
            print(device.HALF_WRITTEN)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
