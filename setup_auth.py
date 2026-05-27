#!/usr/bin/env python3
"""
One-time Garmin Connect authentication setup.
Run this once before launching the dashboard.

If you sign in to Garmin Connect via Google:
  1. Go to connect.garmin.com
  2. Account Settings → Security → Password → set a Garmin-native password
  3. Then run this script with that email and your new Garmin password

Tokens are saved to ~/.garminconnect and reused automatically by the dashboard.
"""
import getpass
import sys
from pathlib import Path

TOKEN_DIR = Path.home() / ".garminconnect"


def main():
    try:
        from garminconnect import Garmin
    except ImportError:
        print("Run `pip install -r requirements.txt` first.")
        sys.exit(1)

    print("Garmin Connect — one-time auth setup")
    print("=" * 42)
    print()
    print("Note for Google Sign-In users:")
    print("  You need a separate Garmin-native password.")
    print("  Set one at: connect.garmin.com → Account Settings → Security")
    print()

    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")

    print("\nAuthenticating with Garmin Connect…")
    try:
        TOKEN_DIR.mkdir(exist_ok=True)
        client = Garmin(email=email, password=password)
        client.login(tokenstore=str(TOKEN_DIR))
        print(f"\nSuccess. Tokens saved to {TOKEN_DIR}")
        print("Now launch the dashboard:  streamlit run app.py")
    except Exception as exc:
        print(f"\nAuthentication failed: {exc}")
        print("Check your credentials and ensure a Garmin-native password is set.")
        sys.exit(1)


if __name__ == "__main__":
    main()
