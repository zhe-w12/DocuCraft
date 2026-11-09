# DocuCraft AI Writing and Editing Workflow

English | [简体中文](ai-writing-workflow.md)

This guide describes how the web app, backend prompts, and product behavior work together for AI writing and editing.

## User entry point

The web app opens on an AI chat page. Users can ask a question directly or choose a workflow: automatic, chat, draft from scratch, edit existing text, proofread, shorten, expand, rewrite, or summarize. Quick actions are available for writing, proofreading, summarization, and chat. Chat mode uses a full-width conversation view and supports natural multi-turn questions without forcing a formal document template. Users can hide the document area and reopen it from the AI panel. After the first request in another workflow, the app shows a document editor alongside the AI panel. The assistant streams formatted text into the document; its title is taken from the response's first-level heading. Users can switch between editing and reading, copy the content, and download Word, PDF, or Markdown files. The app renders Markdown without raw HTML or remote images. On desktop, users can swap or maximize the panels; on mobile, tabs switch between the document and assistant. Image OCR, document history, and the PDF editor remain available from the left side.

## How a request is handled

1. **Collect requirements.** The user describes the goal, source text, purpose, audience, format, length, and tone. If no workflow is selected, the backend infers whether to draft or edit from the instruction and conversation context.
2. **Choose a task pattern.** Drafting can use structures suited to reports, resumes, articles, social posts, speeches, essays, and stories. Editing instructions preserve facts, key points, and required information while applying proofreading, shortening, expansion, rewriting, or summarization.
3. **Check required input.** Editing needs source text and an instruction. If the source is missing, ask the user to provide it. Make reasonable assumptions about nonessential details and state them clearly; do not invent facts, figures, examples, citations, or sources.
4. **Call the model.** The web app sends the current conversation context and workflow marker to the DocuCraft backend. The backend calls the configured model through an OpenAI-compatible Chat Completions API. The model key stays in the server environment.
5. **Review the result.** Prompts ask the model to check relevance, repetition, omissions, and factual drift. If a request is unsafe, the model should decline politely and offer a safer direction. Users should verify generated content, especially professional conclusions and time-sensitive information.
6. **Revise and continue.** Users can insert or replace the document with a response, then request changes such as “make this more formal,” “cut it in half,” or “only revise this paragraph.” The browser saves recent conversations and the document being edited on that device. The current conversation is sent as context for the next request. Unless the user asks for an excerpt, the model returns the full revised text. Word and PDF files are generated temporarily by the backend and removed after the response; Markdown is exported locally in the browser.

## Math formatting

Use standard LaTeX Markdown delimiters: `$...$` for inline math and a separate `$$...$$` line for display math. Do not use bare square brackets as math delimiters. The web app repairs some common legacy formats, but standard delimiters work more reliably with streaming and saved edits.

## Writing and editing boundaries

- **Draft from scratch:** Build a new structure from the user's topic and constraints. Do not present invented details as facts.
- **Edit existing text:** Use the source as the basis. Change expression and structure only as requested, preserving its facts, viewpoints, conditions, and necessary details.
- **Make a targeted edit:** The user should identify the target section or paste the excerpt. Leave the rest unchanged unless the requested change requires otherwise. Preserving meaning does not mean every word must remain identical.
- **Use conversation context:** Use the current conversation for the current request. Conversation records remain in the current browser. When a request is sent, its text passes through the DocuCraft backend to the configured model service.
- **Respect knowledge limits:** A model may not know recent events or figures. Ask the user for source material when current information is needed; the web app does not claim the model has verified facts online.

## Configure the model on the backend

Set the following values in the backend environment file as needed:

```dotenv
AI_PROVIDER=bailian
AI_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
AI_API_KEY=your-server-side-model-key
AI_MODEL=qwen3.8-max
```

Without model configuration, image OCR, document history, and the PDF editor remain available; the AI page shows that setup is required. After configuration, rebuild the backend API container and check its health. Never commit a real `.env` file, credentials, or model request content to GitHub.
