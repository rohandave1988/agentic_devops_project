"""LLM factory used by all agents."""
import config


def build_llm(max_tokens: int = 2048):
    """Return a LangChain chat model that supports tool calling."""
    if config.LLM_BACKEND == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=config.CLAUDE_MODEL,
            api_key=config.ANTHROPIC_KEY,
            temperature=0,
            max_tokens=max_tokens,
        )
    from langchain_ollama import ChatOllama
    return ChatOllama(
        model=config.OLLAMA_MODEL,
        base_url=config.OLLAMA_URL,
        temperature=0,
        num_ctx=8192,        # enough for multi-tool ReAct chains (default 2048 truncates)
        num_predict=max_tokens,
    )
