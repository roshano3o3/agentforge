# Provider response fixtures

Hand-written JSON in the shape each provider's API documents for a response
(OpenAI Chat Completions, Anthropic Messages, Ollama's OpenAI-compatible
endpoint). They are **not** recordings of live calls: no paid model was
called to make them, and their ids, token counts and text are invented.

The tests load each file through the provider SDK's own response model
(`openai.types.chat.ChatCompletion`, `anthropic.types.Message`), so a field
the SDK doesn't accept fails the test, then parse it with
`invoice_agent.llm.parse_openai` / `parse_anthropic`.
