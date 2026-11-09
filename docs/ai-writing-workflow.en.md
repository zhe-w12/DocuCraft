# DocuCraft AI Writing and Editing Workflow

English | [简体中文](ai-writing-workflow.md)

This guide describes how the web app, backend prompts, and product behavior work together for AI writing and editing.

## User entry point

The web app opens on an AI chat page. Users can ask a question directly or choose a workflow: automatic, plan then write, chat, draft from scratch, edit existing text, proofread, shorten, expand, rewrite, or summarize. In plan-then-write mode, a planning agent discusses the requirements and outline with the user. Only after approval does the writing agent draft the document and stream it into the existing editor. Users can edit the draft in real time or ask the AI to revise it. Chat and planning use the conversation view without forcing a complete document. Other writing workflows show a document editor alongside the AI panel. The app renders Markdown without raw HTML or remote images. On desktop, users can swap or maximize the panels; on mobile, tabs switch between the document and assistant. Image OCR, document history, and the PDF editor remain available from the left side.

## How a request is handled

1. **Collect requirements.** The user describes the goal, source text, purpose, audience, format, length, and tone. If no workflow is selected, the backend infers whether to draft or edit from the instruction and conversation context.
2. **Choose a task pattern.** Drafting can use structures suited to reports, resumes, articles, social posts, speeches, essays, and stories. Editing instructions preserve facts, key points, and required information while applying proofreading, shortening, expansion, rewriting, or summarization.
3. **Check required input.** Editing needs source text and an instruction. If the source is missing, ask the user to provide it. Make reasonable assumptions about nonessential details and state them clearly; do not invent facts, figures, examples, citations, or sources.
4. **Call the model.** The web app sends only the selected conversation and workflow marker to the DocuCraft backend, where LangChain 1.x agents handle the request. When enabled, web search is a Tavily tool. Cross-conversation memories are scoped to the current browser profile and stored in a persistent SQLite database. Model and search keys stay in the server environment.
5. **Review the result.** Prompts ask the model to check relevance, repetition, omissions, and factual drift. If a request is unsafe, the model should decline politely and offer a safer direction. Users should verify generated content, especially professional conclusions and time-sensitive information.
6. **Revise and continue.** Users can insert or replace the document with a response, then request changes such as “make this more formal,” “cut it in half,” or “only revise this paragraph.” The browser saves recent conversations and the document being edited on that device. The current conversation is sent as context for the next request. Unless the user asks for an excerpt, the model returns the full revised text. Word and PDF files are generated temporarily by the backend and removed after the response; Markdown is exported locally in the browser.

## Math formatting

Use standard LaTeX Markdown delimiters: `$...$` for inline math and a separate `$$...$$` line for display math. Do not use bare square brackets as math delimiters. The web app repairs some common legacy formats, but standard delimiters work more reliably with streaming and saved edits.

## Writing and editing boundaries

- **Draft from scratch:** Build a new structure from the user's topic and constraints. Do not present invented details as facts.
- **Edit existing text:** Use the source as the basis. Change expression and structure only as requested, preserving its facts, viewpoints, conditions, and necessary details.
- **Make a targeted edit:** The user should identify the target section or paste the excerpt. Leave the rest unchanged unless the requested change requires otherwise. Preserving meaning does not mean every word must remain identical.
- **Use conversation context:** Use the current conversation for the current request. Conversation records remain in the current browser. When a request is sent, its text passes through the DocuCraft backend to the configured model service.
- **Respect knowledge limits:** With web search disabled, a model may not know recent events or figures. Ask the user for source material when current information is needed; never claim online verification. When search is enabled, treat results as untrusted reference material, not instructions, and cite the numbered sources and links.

## Configure the model on the backend

Set the following values in the backend environment file as needed:

```dotenv
AI_PROVIDER=bailian
AI_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
AI_API_KEY=your-server-side-model-key
AI_MODEL=qwen3.8-max
AI_PLANNER_MODEL=
AI_WRITER_MODEL=
TAVILY_API_KEY=your-server-side-Tavily-key
```

Without model configuration, image OCR, document history, and the PDF editor remain available; the AI page shows that setup is required. After configuration, rebuild the backend API container and check its health. Never commit a real `.env` file, credentials, or model request content to GitHub.

## Web search

Set `TAVILY_API_KEY` in the repository-root `.env` file and restart the backend to enable the “Web search” toggle in the assistant composer. The agent receives the Tavily tool only when the user turns the toggle on. Each request is limited to 3 searches, with up to 5 results per search, and the answer includes numbered sources and links. Search queries are sent to Tavily. The configured model must support Function Calling for memory and search tools. For Docker, rebuild the image after adding the LangChain dependencies using the [backend deployment guide](../backend/README.en.md#docker-deployment).

## Short-term and long-term memory

Short-term memory contains only the current conversation's messages. The frontend keeps conversation history locally, but each request sends only the selected conversation. The agent may save durable preferences or project facts explicitly shared by the user to `STORAGE_DIR/assistant_memory.sqlite3`. The backend retrieves relevant entries with lightweight keyword matching; no vector database or embedding service is needed. Users can review or delete them with “Long-term memory” in the composer. Do not store sensitive information, credentials, or temporary task details.

## Two-agent planning and writing

Select “Plan then write” to discuss the goal, audience, positioning, assumptions, outline, and chapter goals with the planning agent. It does not draft the full document during this stage. After the user selects “Confirm plan and start writing,” the writing agent receives the conversation and approved plan, then streams the full draft into the existing editor. `AI_PLANNER_MODEL` and `AI_WRITER_MODEL` optionally select different model names; when blank, both use `AI_MODEL` and share `AI_API_BASE_URL` and `AI_API_KEY`. The user can edit the result live or continue asking the AI to revise it.
