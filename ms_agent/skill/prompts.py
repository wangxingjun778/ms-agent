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
