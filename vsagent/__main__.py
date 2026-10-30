from vsagent.app import VSAgentApp


def main() -> None:
    """启动 VSAgent。模型接口参数由应用内 `/model` 填写并存在 `connection.toml`。"""
    VSAgentApp().run()


if __name__ == "__main__":
    main()
