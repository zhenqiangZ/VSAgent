from opencode_tui.app import OpenCodeApp


def main() -> None:
    """启动 opencode TUI。模型接口参数由应用内 `/model` 填写并存在 `connection.toml`。"""
    OpenCodeApp().run()


if __name__ == "__main__":
    main()
