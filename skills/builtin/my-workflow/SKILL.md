---
name: my-workflow
description: My custom daily workflow automation — organize tasks, generate standup notes, and format code. This is a personal skill I created for my own use.
license: MIT
version: "1.0"
---
# My Daily Workflow Skill

## Purpose
Automate my common daily routines so I don't have to repeat the same instructions every day.

## Workflows

### Morning Standup
When I say "standup" or "daily update":
1. List what I worked on yesterday (check git log for the past 24 hours)
2. Summarize current branches and their status
3. Generate a standup-format summary

### Task Organization
When I say "organize tasks" or "what should I work on":
1. Read any TODO.md or task files in the workspace
2. Prioritize by urgency and dependencies
3. Output a sorted task list with estimated effort

### Code Format Check
When I say "check format" or "lint check":
1. Run the appropriate linter for the project language
2. Report issues grouped by severity
3. Offer to auto-fix where possible

## Preferences
- Use concise language, not verbose explanations
- Always show the command that will be run before executing
- For git operations, never force-push to main/master

## Templates
See `templates/standup_template.md` for the standup format.
