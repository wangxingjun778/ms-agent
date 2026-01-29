# flake8: noqa
# yapf: disable

DEFAULT_PLAN = """

"""

DEFAULT_TASKS = """

"""

DEFAULT_IMPLEMENTATION = """

"""


PROMPT_SKILL_PLAN = """
According to the user's request:\n {query}\n,
analyze the following skill content and breakdown the necessary steps to complete the task step by step, considering any dependencies or prerequisites that may be required.
According to following sections: `SKILL_MD_CONTEXT`, `REFERENCE_CONTEXT`, `SCRIPT_CONTEXT` and `RESOURCE_CONTEXT`, you **MUST** identify the most relevant **FILES** (if any) and outline a detailed plan to accomplish the user's request.
{skill_md_context} {reference_context} {script_context} {resource_context}
\n\nThe format of your response:\n
<QUERY>
... The user's original query ...
</QUERY>


<PLAN>
... The concise and clear step-by-step plan to accomplish the user's request ...
</PLAN>


<SCRIPTS>
... The most relevant SCRIPTS (if any) in JSON format ...
</SCRIPTS>


<REFERENCES>
... The most relevant REFERENCES (if any) in JSON format ...
</REFERENCES>


<RESOURCES>
... The most relevant RESOURCES (if any) in JSON format ...
</RESOURCES>

"""


PROMPT_SKILL_TASKS = """
According to `SKILL PLAN CONTEXT`:\n\n{skill_plan_context}\n\n
Provide a concise and precise TODO-LIST of implementations required to execute the plan, **MUST** be as concise as possible.
Each task should be specific, actionable, and clearly defined to ensure successful completion of the overall plan.
The format of your response: \n
<QUERY>
... The user's original query ...
</QUERY>


<TASKS>
... A concise and clear TODO-LIST of implementations required to execute the plan ...
</TASKS>

"""


SCRIPTS_IMPLEMENTATION_FORMAT = """[
    {
        "script": "<script_path_1>",
        "parameters": {
            "param1": "value1",
            "param2": "value2"
        }
    },
    {
        "script": "<script_path_2>",
        "parameters": {
            "param1": "value1",
            "param2": "value2"
        }
    }
]"""

PROMPT_TASKS_IMPLEMENTATION = """
According to relevant content of `SCRIPTS`, `REFERENCES` and `RESOURCES`:\n\n{script_contents}\n\n{reference_contents}\n\n{resource_contents}\n\n

You **MUST** strictly implement the todo-list in `SKILL_TASKS_CONTEXT` step by step:\n\n{skill_tasks_context}\n\n

There are 3 scenarios for response, your response **MUST** strictly follow one of the above scenarios, **MUST** be as concise as possible:

Scenario-1: Execute Script(s) with Parameters, especially for python scripts, in the format of:
<IMPLEMENTATION>
{scripts_implementation_format}
</IMPLEMENTATION>

Scenario-2: No Script Execution Needed, like JavaScript、HTML code generation, please output the final answer directly, in the format of:
<IMPLEMENTATION>
```html
```
...
or
```javascript
```
</IMPLEMENTATION>

Scenario-3: Unable to Execute Any Script, Provide Reason, in the format of:
<IMPLEMENTATION>
... The reason why unable to execute any script ...
</IMPLEMENTATION>

"""


PROMPT_SKILL_FINAL_SUMMARY = """
Given the comprehensive context:\n\n{comprehensive_context}\n\n
Provide a concise summary of the entire process, highlighting key actions taken, decisions made, and the final outcome achieved.
Ensure the summary is clear and informative.
"""


# ============================================================
# AutoSkills Prompts - for automatic skill retrieval and DAG
# ============================================================

PROMPT_ANALYZE_QUERY_FOR_SKILLS = """You are a skill analyzer. Given a user query, identify what types of skills/capabilities are needed, or just chatting is sufficient.

User Query: {query}

Available Skills Overview:
{skills_overview}

Analyze the query and determine:
1. Whether this query requires specific skills/capabilities to fulfill
2. If skills are needed, what capabilities/functions are directly required
3. What prerequisites or dependencies might be required

Output in JSON format:
{{
    "needs_skills": true/false,
    "intent_summary": "Brief description of user intent",
    "skill_queries": ["query1", "query2", ...],
    "chat_response": "Direct response if no skills needed, null otherwise",
    "reasoning": "Brief explanation"
}}

Notes:
- Set `needs_skills` to false if the query is casual chat, greeting, or can be answered directly without special skills.
- If `needs_skills` is false, provide the `chat_response` with a helpful direct answer.
- If `needs_skills` is true, `skill_queries` should contain search queries for finding relevant skills.
"""

PROMPT_EVALUATE_SKILLS_COMPLETENESS = """You are evaluating if the retrieved skills are sufficient to complete a user task.

User Query: {query}
Intent Summary: {intent_summary}

Retrieved Skills:
{retrieved_skills}

Evaluate:
1. Can these skills collectively fulfill the user's request?
2. Are there any missing capabilities or dependencies?
3. Is there any gap that needs additional skills?

Output in JSON format:
{{
    "is_complete": true/false,
    "missing_capabilities": ["capability1", ...],
    "additional_queries": ["query1", ...],
    "clarification_needed": null or "question to ask user if unable to proceed"
}}
"""

PROMPT_BUILD_SKILLS_DAG = """You are building a dependency graph (DAG) for executing skills.

User Query: {query}

Selected Skills:
{selected_skills}

Build an execution DAG where:
- Each skill is a node identified by skill_id
- Edges represent dependencies (A -> B means A must complete before B)
- Consider logical execution order and data dependencies

Output in JSON format:
{{
    "dag": {{
        "skill_id_1": ["dependent_skill_id_a", "dependent_skill_id_b"],
        "skill_id_2": [],
        ...
    }},
    "execution_order": ["skill_id_1", ["skill_id_2", "skill_id_3"], "skill_id_4", ...],
    "reasoning": "Brief explanation of the dependency structure"
}}

Notes:
    The `execution_order` can include parallel execution steps represented as lists.
    The `execution_order` must respect the dependencies defined in the `dag`.


You MUST follow principles:
- Minimal Sufficiency Principle: Choose the smallest set of skills that fully satisfies the user's query—no extra or unnecessary skills should be included.
- Skill Deduplication: If multiple skills serve similar or overlapping purposes, retain only the most effective or optimal one and remove redundant alternatives.
"""

PROMPT_DIRECT_SELECT_SKILLS = """You are a skill selector. Given a user query and all available skills, select the relevant skills and build an execution DAG.

User Query: {query}

All Available Skills:
{all_skills}

Tasks:
1. Determine if this query needs skills or is just casual chat
2. If skills are needed, select ALL relevant skills from the list above
3. Build a dependency DAG for the selected skills

Output in JSON format:
{{
    "needs_skills": true/false,
    "chat_response": "Direct response if no skills needed, null otherwise",
    "selected_skill_ids": ["skill_id_1", "skill_id_2", ...],
    "dag": {{
        "skill_id_1": ["dependent_skill_id_a", "dependent_skill_id_b"],
        "skill_id_2": [],
        ...
    }},
    "execution_order": ["skill_id_1", ["skill_id_2", "skill_id_3"], "skill_id_4", ...],
    "reasoning": "Brief explanation of skill selection and dependencies"
}}

Notes:
- Set `needs_skills` to false if the query is casual chat or can be answered directly.
- Only include skill_ids that exist in the available skills list.
- The `execution_order` can include parallel execution steps represented as lists.
"""

# ============================================================
# Progressive Skill Analysis Prompts
# ============================================================

PROMPT_VALIDATE_SKILL_RELEVANCE = """You are validating if a skill is truly necessary for the user's query.

User Query: {query}

Skill Being Evaluated:
- Skill ID: {skill_id}
- Name: {skill_name}
- Description: {skill_description}

Skill Content Summary:
{skill_content}

Other Selected Skills in DAG:
{other_skills}

Evaluation Criteria:
1. Does this skill directly contribute to fulfilling the user's query?
2. Is this skill's functionality already covered by other selected skills?
3. Is this skill redundant or unnecessary for the specific task?
4. Does this skill match the user's actual intent?

Output in JSON format:
{{
    "is_relevant": true/false,
    "is_redundant": true/false,
    "redundant_with": "skill_id that provides similar functionality (if redundant)",
    "relevance_score": 0.0-1.0,
    "reason": "Brief explanation of the evaluation",
    "recommendation": "keep|remove|replace_with_skill_id"
}}

Notes:
- Set `is_relevant` to false if the skill doesn't directly help with the query.
- Set `is_redundant` to true if another skill already covers this functionality.
- A skill should be removed if relevance_score < 0.5 or if it's redundant.
"""


PROMPT_SKILL_ANALYSIS_PLAN = """You are analyzing a skill to create an execution plan.

User Query: {query}

Skill Information:
- Skill ID: {skill_id}
- Name: {skill_name}
- Description: {skill_description}

Skill Content (SKILL.md):
{skill_content}

Available Resources Overview:
- Scripts: {scripts_list}
- References: {references_list}
- Resources: {resources_list}

Tasks:
1. Understand what this skill can do based on its description and content
2. Determine if this skill can address the user's query
3. Create a step-by-step execution plan
4. Identify which scripts, references, and resources are needed

Output in JSON format:
{{
    "can_handle": true/false,
    "plan_summary": "Brief summary of the execution plan",
    "steps": [
        {{"step": 1, "action": "description", "type": "script|reference|resource|code"}},
        ...
    ],
    "required_scripts": ["script_name1", "script_name2", ...],
    "required_references": ["ref_name1", ...],
    "required_resources": ["resource_name1", ...],
    "required_packages": ["python_package1", "python_package2", ...],
    "parameters": {{"param1": "value or <user_input>", ...}},
    "reasoning": "Why this plan will work"
}}

Notes:
- Only include resources that are actually needed for execution.
- Steps should be actionable and specific.
- Parameters should include any values extracted from the query.
- Extract Python package dependencies from skill content (e.g., reportlab, pandas, numpy).
"""

PROMPT_CLARIFY_USER_INTENT = """You are verifying if the selected skills can fully satisfy the user's intent.

User Query: {query}

Selected Skills:
{selected_skills}

Tasks:
1. Analyze the user's intent and requirements from the query
2. Evaluate if the selected skills can completely fulfill the user's needs
3. Identify any gaps or missing capabilities
4. If clarification is needed, formulate a clear question for the user

Output in JSON format:
{{
    "intent_satisfied": true/false,
    "intent_summary": "Brief summary of what user wants to achieve",
    "coverage_analysis": {{
        "covered": ["capability1 covered by skill_x", ...],
        "missing": ["missing capability1", ...]
    }},
    "confidence": 0.0-1.0,
    "clarification_needed": null or "Specific question to ask the user",
    "suggestion": "Optional suggestion for the user if clarification is needed"
}}

Notes:
- Set `intent_satisfied` to true only if you are confident (>0.8) skills can fulfill the query.
- If `intent_satisfied` is false, provide a clear `clarification_needed` question.
- The question should help gather missing information to better match skills.
"""


PROMPT_SKILL_EXECUTION_COMMAND = """Based on the execution plan and loaded resources, generate the execution command(s).

User Query: {query}
Skill ID: {skill_id}

Execution Plan:
{execution_plan}

Loaded Scripts:
{scripts_content}

Loaded References:
{references_content}

Loaded Resources:
{resources_content}

Generate the specific execution command(s) needed.

Output in JSON format:
{{
    "execution_type": "script|code|shell",
    "commands": [
        {{
            "type": "python_script|python_code|shell|javascript",
            "path": "script_path (if applicable)",
            "code": "inline code (if applicable)",
            "parameters": {{"param1": "value", ...}},
            "working_dir": "working directory (optional)",
            "requirements": ["package1", "package2", ...]
        }},
        ...
    ],
    "expected_output": "Description of expected output"
}}
"""
