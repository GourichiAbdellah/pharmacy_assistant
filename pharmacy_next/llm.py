from openai import AsyncOpenAI


class ModelUnavailable(RuntimeError):
    pass


class ModelClient:
    def __init__(self, settings):
        self.settings = settings
        self.client = None

    async def complete(self, messages, tools):
        s = self.settings
        if not s.llm_api_key:
            raise ModelUnavailable("Configure LLM_API_KEY dans .env pour démarrer le dialogue.")
        if self.client is None:
            self.client = AsyncOpenAI(api_key=s.llm_api_key, base_url=s.llm_base_url,
                                      timeout=s.llm_timeout_seconds, max_retries=1)
        parameters = {"model": s.llm_model, "messages": messages, "tools": tools,
                      "tool_choice": "auto", "max_tokens": 1800}
        if s.llm_provider == "deepseek":
            parameters["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            response = await self.client.chat.completions.create(**parameters)
        except Exception:
            # Never forward SDK errors containing credentials/endpoints/request bodies.
            raise ModelUnavailable("Le modèle est indisponible. Vérifie l'endpoint, la clé, le nom du modèle et les quotas.") from None
        choice = response.choices[0]
        if choice.finish_reason in {"length", "content_filter"}:
            raise ModelUnavailable("La génération n'a pas pu être terminée. Réessaie dans quelques instants.")
        message = choice.message
        result = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            result["tool_calls"] = [call.model_dump(exclude_none=True) for call in message.tool_calls]
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning is not None:
            result["reasoning_content"] = reasoning
        return result

    async def close(self):
        if self.client is not None:
            await self.client.close()
