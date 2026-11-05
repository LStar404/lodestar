export type EditCommand = "bold" | "italic" | "heading" | "list" | "quote" | "math" | "code" | "link";

/** Return a single replacement so browsers can preserve native undo history. */
export function markdownEdit(text: string, start: number, end: number, command: EditCommand) {
  const selected = text.slice(start, end);
  if (command === "heading" || command === "list" || command === "quote") {
    const from = start === 0 ? 0 : text.lastIndexOf("\n", start - 1) + 1;
    const to = end > start && text[end - 1] === "\n" ? end - 1 : end;
    const prefix = command === "heading" ? "## " : command === "list" ? "- " : "> ";
    const replacement = text.slice(from, to).split("\n").map(line => prefix + line).join("\n");
    return { from, to, replacement, selectionStart: from + prefix.length, selectionEnd: from + replacement.length };
  }
  const pairs = {
    bold: ["**", "**", "加粗文字"], italic: ["*", "*", "斜体文字"],
    math: ["$", "$", "x^2"], code: ["`", "`", "代码"], link: ["[", "](https://)", "链接文字"],
  };
  const [before, after, placeholder] = pairs[command];
  const body = selected || placeholder;
  return { from: start, to: end, replacement: before + body + after,
    selectionStart: start + before.length, selectionEnd: start + before.length + body.length };
}

export interface EditorText { title: string; content: string }
export const sameText = (a: EditorText, b: EditorText) => a.title === b.title && a.content === b.content;

/** A response may normalize text; only adopt fields the user has not edited since sending. */
export function mergeSavedText(current: EditorText, sent: EditorText, saved: EditorText): EditorText {
  return {
    title: current.title === sent.title ? saved.title : current.title,
    content: current.content === sent.content ? saved.content : current.content,
  };
}
