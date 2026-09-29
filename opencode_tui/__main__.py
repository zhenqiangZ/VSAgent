from opencode_tui.app import OpenCodeApp


def main() -> None:
    """启动 opencode TUI。密钥只从项目根目录的 `.env` 读。"""
    OpenCodeApp().run()


if __name__ == "__main__":
    main()
