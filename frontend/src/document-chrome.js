import { stripDocumentChrome } from "./markdown-rendering.js";

export function cleanActiveDocument(state, conversation) {
  const content = stripDocumentChrome(state.documentContent);
  if (content === state.documentContent) return false;

  state.documentContent = content;
  if (conversation) {
    conversation.document = { title: state.documentTitle, content };
  }
  return true;
}
