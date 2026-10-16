from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()  # reads ANTHROPIC_API_KEY from .env

MODEL = "claude-sonnet-5-5"


def main():
    client = Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[
            {"role": "user", "content": "In one sentence, what is an always-on agent?"}
        ],
    )
    print(response.content[0].text)
    print(f"Tokens used: {response.usage.input_tokens} in, {response.usage.output_tokens} out")


if __name__ == "__main__":
    main()

  