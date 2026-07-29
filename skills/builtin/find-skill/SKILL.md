---
name: find-skill
description: Discover and load the right skill for the task at hand. Use this skill when you're unsure which skills are available, need to find a skill by capability or keyword, or want to check if a specialized workflow exists before building one from scratch.
license: MIT
version: "1.0"
---

# Find Skill

## Purpose

Discover available skills that match the current task. This skill prevents reinventing the wheel — before manually executing a complex workflow, check if a skill already provides optimized instructions for it.

## When to Use

- User asks for something that sounds like a repeatable workflow (code review, research, deployment, architecture review, etc.)
- You're about to spend significant effort on a multi-step task
- User mentions a skill name but you're not sure it exists
- You want to see all skills across a certain domain

## Workflow

### Step 1: Scan available skills

Use `file_read` or `Glob` to list available skills:

```
Glob: /mnt/skills/builtin/*/SKILL.md
Glob: /mnt/skills/my/*/SKILL.md
```

Each directory name is the skill name; each `SKILL.md` contains the skill's description and workflow.

### Step 2: Match by description

For skills whose name doesn't obviously match the task, read their `SKILL.md` frontmatter to get the description. The YAML frontmatter is between `---` fences at the top:

```yaml
---
name: skill-name
description: What this skill does
license: MIT
---
```

Key matching signals:
1. **Skill name** — exact or fuzzy keyword match against the user's request
2. **Description** — semantic fit with the task requirements
3. **Domain overlap** — the skill's category matches the task's domain

### Step 3: Load the best match

Once a skill is identified, call `file_read` on its SKILL.md location to load the full workflow instructions. Follow the skill's progressive loading pattern — read referenced resources (references/, templates/, scripts/) only when needed.

### Step 4: Report or execute

- **Single clear match**: Load the skill and follow its workflow without further confirmation
- **Multiple candidates**: Present a brief summary to the user: "I found skills for A, B, and C — which should I use?"
- **No match**: Tell the user "No built-in skill matches this task. I'll proceed manually. To create a skill for this, use skill_manage."

## Skill Categories

Built-in skills are located at `/mnt/skills/builtin/`. User-created skills are at `/mnt/skills/my/`. Built-in skills are read-only; user skills can be edited.

## Quick Reference

| User says... | Look for... |
|---|---|
| "review this code" | `code-reviewer` |
| "research X" / "write a report" | `deep-research` |
| "review the architecture" | `system-architecture-review` |
| "check before deploying" | `deployment-checklist` |
| "what skills are available" | List all `*/SKILL.md` files |
