User Query
    │
    ▼
┌─────────────────────────────────────────┐
│  Phase 1: Plan Analysis                  │
│  - Load skill.name + description + content │
│  - Analyze with LLM                      │
│  - Create SkillExecutionPlan             │
│  - Identify required: scripts/refs/resources │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│  Phase 2: Selective Resource Loading     │
│  - Load ONLY required scripts            │
│  - Load ONLY required references         │
│  - Load ONLY required resources          │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│  Phase 3: Command Generation & Execution │
│  - Generate execution commands           │
│  - Execute via SkillContainer            │
│  - Link outputs for downstream skills    │
└─────────────────────────────────────────┘
