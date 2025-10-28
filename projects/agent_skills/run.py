# flake8: noqa
# yapf: disable
# Copyright (c) Alibaba, Inc. and its affiliates.
import os
from pathlib import Path

from ms_agent.skill import create_agent_skill

_PATH = Path(__file__).parent.resolve()


def main():
    """
    Main function to create and run an agent with specified skills.

    NOTES:
        1. Configure the working directory, skill root path, and model name as needed.
        2. Configure the `OPENAI_API_KEY` and `OPENAI_BASE_URL` environment variables for API access.
    """
    working_dir: str = str(_PATH / 'temp_workspace')
    skill_root_path: str = str(_PATH / 'skills')
    example_data_dir: str = str(_PATH / 'example_data')
    model_name: str = 'qwen-plus-latest'

    agent = create_agent_skill(
        skills=skill_root_path,
        model=model_name,
        api_key=os.getenv('OPENAI_API_KEY'),
        base_url=os.getenv('OPENAI_BASE_URL'),
        stream=True,
        working_dir=working_dir,
    )

    queries = [
        f'Extract the form field info from pdf: {example_data_dir}/OLYMPIC_MEDAL_TABLE_zh.pdf, note that you need to check it first. Finally, output to the working directory: {working_dir} as proper file name with json extension.',
        # "Create generative art using p5.js with seeded randomness, flow fields, and particle systems, please fill in the details and provide the complete code based on the templates."
    ]

    for query in queries:
        print(f"\n{'= ' * 60}")
        print(f'User query: {query}\n\n')
        response = agent.run(query)
        print(f'\n\nAgent skill results: {response}\n')


if __name__ == '__main__':

    main()
