"""Pretend entry point. Lives in test-folder/src/ to prove subdir walking."""


def main() -> int:
    print("hello from test-folder/src/main.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
