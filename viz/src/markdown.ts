// Markdown 渲染的打包入口。会话正文与工具输出都是 Markdown，
// 项目规则不允许手写解析器，所以用 marked 解析、DOMPurify 清理。
//
// 清理这一步是必要的：工具输出里可能带任意文件的内容，
// 里面的 HTML 标签如果直接插进页面就会执行。
import { marked } from "marked";
import DOMPurify from "dompurify";

export function renderMarkdown(text: string): string {
  const html = marked.parse(text, {
    async: false,
    gfm: true,
    breaks: true,
  }) as string;
  return DOMPurify.sanitize(html);
}

// 工具调用的输入是 JSON 对象，按等宽文本展示。
export function renderJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
