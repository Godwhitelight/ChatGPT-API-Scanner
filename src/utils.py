"""
This module provides a function to check the validity of an OpenAI API key by making a test request
to a chosen model using the OpenAI client. It captures various exceptions and prints error details.
"""

import rich
from openai import APIStatusError, AuthenticationError, OpenAI, RateLimitError


def check_key(key, model="gpt-4o-mini") -> str | None:
    """
    Check if the API key is valid.
    """
    try:
        client = OpenAI(api_key=key)

        completion = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a yeser, you only output lowercase yes.",
                },
                {"role": "user", "content": "yes or no? say yes"},
            ],
        )
        result = completion.choices[0].message.content
        rich.print(f"🔑 [bold green]available key[/bold green]: [orange_red1]'{key}'[/orange_red1] ({result})\n")
        return "yes"
    except AuthenticationError as e:
        rich.print(f"[deep_sky_blue1]{e.body['code']} ({e.status_code})[/deep_sky_blue1]: '{key[:10]}...{key[-10:]}'")  # type: ignore
        return e.body["code"]  # type: ignore
    except RateLimitError as e:
        rich.print(f"[deep_sky_blue1]{e.body['code']} ({e.status_code})[/deep_sky_blue1]: '{key[:10]}...{key[-10:]}'")  # type: ignore
        return e.body["code"]  # type: ignore
    except APIStatusError as e:
        rich.print(f"[bold red]{e.body['code']} ({e.status_code})[/bold red]: '{key[:10]}...{key[-10:]}'")  # type: ignore
        return e.body["code"]  # type: ignore
    except Exception as e:  # pylint: disable=broad-except
        rich.print(f"[bold red]{e}[/bold red]: '{key[:10]}...{key[-10:]}'")  # type: ignore
        return "Unknown Error"


if __name__ == "__main__":
    check_key("sk-proj-12345")
