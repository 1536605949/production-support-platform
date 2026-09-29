import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--dataset-id', required=True)
    parser.add_argument('--tool-provider-id', required=True)
    parser.add_argument('--reviewer-tool-provider-id', required=True)
    args = parser.parse_args()

    text = Path(args.input).read_text(encoding='utf-8')
    replacements = {
        '__SUPPORT_KNOWLEDGE_DATASET_ID__': args.dataset_id,
        '__BUSINESS_TOOL_PROVIDER_ID__': args.tool_provider_id,
        '__REVIEWER_TOOL_PROVIDER_ID__': args.reviewer_tool_provider_id,
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    if any(placeholder in text for placeholder in replacements):
        raise SystemExit('placeholder replacement incomplete')
    Path(args.output).write_text(text, encoding='utf-8')
    print('Wrote', args.output)


if __name__ == '__main__':
    main()
