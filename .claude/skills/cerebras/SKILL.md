---
name: cerebras
description: Use this to write code to call an LLM using LiteLLM and Opencode Go with the Cerebras inference provider.
---

# Calling an LLM via Cerebras

These instructions allow you write code to call an LLM with Cerebras specified as the inference provider. This method uses LiteLLM and Opencode Go.

## Setup

The `OPENCODE_API_KEY` environment variable must be set to your OpenCode API key and it must be set in the `.env` file and loaded in as an environment variable.

The uv project must include litellm and pydantic:
`uv add litellm pydantic`

## Code snippets

Use code like these examples in order to use Cerebras.

### Imports and constants

```python
import os
from litellm import completion
MODEL = "opencode/deepseek-v4-flash-free"
API_BASE = "https://opencode.ai/zen/v1"
API_KEY = os.environ["OPENCODE_API_KEY"]
EXTRA_BODY = {"provider": {"order": ["cerebras"]}}
```

### Code to call via Cerebras for a text response

```python
response = completion(model=MODEL, messages=messages, reasoning_effort="low", extra_body=EXTRA_BODY, api_base=API_BASE, api_key=API_KEY)
result = response.choices[0].message.content
```

### Code to call via Cerebras for a Structured Outputs response

```python
response = completion(model=MODEL, messages=messages, reasoning_effort="low", extra_body=EXTRA_BODY, api_base=API_BASE, api_key=API_KEY, response_format=MyBaseModelSubclass)
result = response.choices[0].message.content
result_as_object = MyBaseModelSubclass.model_validate_json(result)
```
