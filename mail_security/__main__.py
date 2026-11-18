"""支持 ``python -m mail_security`` 调用 CLI。"""

from mail_security.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
