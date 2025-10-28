# Agent Skills

Empower your AI agents with a modular skill framework that supports dynamic skill discovery, progressive context loading and secure script execution.

## Overview

The Agent Skills Framework implements a multi-level progressive context loading mechanism that efficiently manages skill discovery and execution:

1. **Level 1 (Metadata)**: Load all skill names and descriptions
2. **Level 2 (Retrieval)**: Retrieve and load SKILL.md when relevant with the query
3. **Level 3 (Resources)**: Load additional files (references, scripts, resources) only when referenced in SKILL.md
4. **Level 4 (Analysis and Execution)**: Analyze the loaded skill context and execute scripts as needed

This approach minimizes resource consumption while providing comprehensive skill capabilities.


### Core Components

| Component        | Description                                 |
|------------------|---------------------------------------------|
| `AgentSkill`     | Main agent class implementing pipeline      |
| `SkillLoader`    | Loads and manages skill definitions         |
| `Retriever`      | Finds relevant skills using semantic search |
| `SkillContext`   | Builds execution context for skills         |
| `ScriptExecutor` | Safely executes skill scripts               |
| `SkillSchema`    | Schema for skill definitions                |

## Key Features

### Progressive Context Loading
- **Compatibility**: Full compatible with [Anthropic Skills](https://github.com/anthropics/skills) Protocol
- **Efficient Resource Usage**: Only loads necessary files when needed
- **Scalable Design**: Supports hundreds of skills without performance degradation
- **Dynamic Discovery**: Automatically discovers new skills in directories

### Secure Script Execution
- **Sandbox Environment**: Optional isolated execution environment
- **Package Management**: Automatic dependency installation

### Flexible Skill Structure
- **Standard Format**: Consistent skill definition structure
- **Multiple File Types**: Support for documentation, scripts, and resources
- **Extensible Design**: Easy to add new skill types

## Installation

### Prerequisites
- Python 3.8+
- pip package manager

### Install from PyPI
```bash
pip install ms-agent -U
```

### Install from Source
```bash
git clone git@github.com:modelscope/ms-agent.git
cd ms-agent
pip install -r requirements.txt
```

### Environment Variables
```bash
export OPENAI_API_KEY="your-api-key"
export OPENAI_BASE_URL="your-base-url"
```

## Quick Start

### Usage

```python
import os

from ms_agent.skill import create_agent_skill

def main():
    """
    Main function to create and run an agent with specified skills.

    NOTES:
        1. Configure the working directory, skill root path, and model name as needed.
        2. Configure the `OPENAI_API_KEY` and `OPENAI_BASE_URL` environment variables for API access.
    """
    working_dir: str = '/path/to/your_working_dir'
    skill_root_path: str = '/path/to/skills'
    model_name: str = 'qwen-plus-latest'

    agent = create_agent_skill(
        skills=skill_root_path,
        model=model_name,
        api_key=os.getenv('OPENAI_API_KEY'),
        base_url=os.getenv('OPENAI_BASE_URL'),
        stream=True,
        working_dir=working_dir,
    )

    query = "Create generative art using p5.js with seeded randomness, flow fields, and particle systems, please fill in the details and provide the complete code based on the templates."
    response = agent.run(query)
    print(f'\n\nAgent skill results: {response}\n')


if __name__ == '__main__':

    main()

```


## Skill Structure

For more details on skill structure and definitions, refer to:
[Anthropic Agent-Skills](https://docs.claude.com/en/docs/agents-and-tools/agent-skills)

### Directory Layout
```
skill-name/
├── SKILL.md              # Main skill definition
├── reference.md          # Detailed reference material
├── LICENSE.txt           # License information
├── resources/            # Additional resources
│   ├── template.xlsx     # Example files
│   └── data.json         # Data files
└── scripts/              # Executable scripts
    ├── main.py           # Main implementation
    └── helper.py         # Helper functions
```

### SKILL.md Format
```markdown
---
name: "Skill Name"
description: "Brief description of the skill"
tags: ["tag1", "tag2", "tag3"]
author: "Author Name"
version: "1.0.0"
dependencies: ["numpy", "pandas"]
---

# Skill Title

Detailed explanation of what the skill does...

## Key Features

- Feature 1
- Feature 2
- Feature 3

## Usage

Instructions on how to use this skill...

## Examples

```
