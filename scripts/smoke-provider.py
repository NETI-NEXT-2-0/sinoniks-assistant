#!/usr/bin/env python3
"""Run a bounded provider profile/Responses smoke without persisting secrets."""
import asyncio
import json

from partner_agent.infra.settings import Settings
from partner_agent.runtime.provider import (
    capability_probe,
    chat_completion,
    responses_result,
)


async def main() -> None:
    settings = Settings()
    probe = await capability_probe(settings)
    body = await chat_completion(
        settings, [{"role": "user", "content": 'Return JSON only: {"ok":true}'}]
    )
    text, usage = (
        responses_result(body) if settings.model_wire_api == "responses"
        else (str(body), body.get("usage", {}))
    )
    result = {"reachable": probe.get("reachable"), "model": probe.get("model"),
              "wire_api": settings.model_wire_api, "text": text[:500], "usage": usage}
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    asyncio.run(main())
