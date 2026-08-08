"""Background skill review prompts — adapted from Hermes.

These prompts are fed to the background review fork subagent.  The fork
uses progressive loading: ``skill_read(path)`` to load a skill's full
SKILL.md content, then ``skill_manage`` to create/patch/delete.
It runs silently — no user-visible output.
"""

SKILL_REVIEW_PROMPT = """\
Review the conversation above and update the skill library. Be ACTIVE —
most sessions produce at least one skill update, even if small. A pass
that does nothing is a missed learning opportunity, not a neutral outcome.

WORKFLOW: use `skill_read(path)` to load a skill's content BEFORE you
patch it. The Skills Catalog shows names, descriptions, and read paths —
call `skill_read` on any skill you want to inspect or update. This is
the same progressive-loading pattern the main agent uses with `file_read`.

Target shape of the library: CLASS-LEVEL skills, each with a rich SKILL.md
and a `references/` directory for session-specific detail. Not a long flat
list of narrow one-session-one-skill entries. This shapes HOW you update,
not WHETHER you update.

Signals to look for (any one of these warrants action):
  • User corrected your style, tone, format, legibility, or verbosity.
    Frustration signals like "stop doing X", "this is too verbose", "don't
    format like this", "why are you explaining", "just give me the answer",
    or an explicit "remember this" are FIRST-CLASS skill signals. Update
    the relevant skill(s) to embed the preference so the next session
    starts already knowing.
  • User corrected your workflow, approach, or sequence of steps. Encode
    the correction as a pitfall or explicit step in the governing skill.
  • Non-trivial technique, fix, workaround, debugging path, or tool-usage
    pattern emerged that a future session would benefit from. Capture it.
  • A skill that got loaded or consulted this session turned out to be
    wrong, missing a step, or outdated. Patch it NOW.

Choosing WHERE an update goes — the domain-match rule decides FIRST:
  • An existing skill is a valid home ONLY when the new lesson falls
    squarely inside that skill's stated domain (check its description).
    "It involves the same system" or "it happened in the same session"
    is NOT a domain match — a lesson about search-query analysis does
    not belong in a skill about subagent orchestration just because a
    subagent did the analysis.
  • NEVER broaden a skill's description or scope just to absorb an
    unrelated lesson. Scope creep turns one skill into a junk drawer:
    the description stops matching its content, and future sessions can
    no longer tell when to load it.
  • If no existing skill's domain covers the lesson, CREATE a new
    class-level skill. Creating a well-scoped new skill is a first-class
    outcome — the library is healthiest as a set of focused skills, not
    one bloated catch-all.

Preference order — after the domain-match rule, prefer the earliest
action that fits, but do pick one when a signal above fired:
  1. UPDATE A CURRENTLY-LOADED SKILL whose domain matches. Look back
     through the conversation for skills the agent read via file_read on
     /mnt/skills/builtin/... or /mnt/skills/my/... paths. If one covers
     the territory of the new learning, PATCH that one first. It was in
     play; it's the right place to extend.
  2. UPDATE AN EXISTING SKILL whose domain matches. Add a subsection, a
     pitfall, or broaden a trigger.
  3. ADD A SUPPORT FILE under a domain-matching skill. Skills can be
     packaged with three kinds of support files — use the right directory:
       • `references/<topic>.md` — session-specific detail (error
         transcripts, reproduction recipes, provider quirks) AND condensed
         knowledge banks (quoted research, API excerpts, domain notes).
       • `templates/<name>.<ext>` — starter files meant to be copied and
         modified (boilerplate, scaffolding, known-good examples).
       • `scripts/<name>.<ext>` — statically re-runnable actions
         (verification, fixture generators, deterministic probes).
     Add support files via skill_manage action=write_file. The skill's
     SKILL.md should gain a one-line pointer to any new support file so
     future agents know it exists.
  4. CREATE A NEW CLASS-LEVEL SKILL when no existing skill's domain
     covers the lesson. The name MUST be at the class level — NOT a
     specific PR number, error string, codename, or "fix-X / debug-Y"
     session artifact. If the name only fits today's task, fall back to
     (1), (2), or (3).

Keep SKILL.md organized: prefer integrating new content into the right
section (replace_section) over blind appends, and every append must go
under a fitting heading. If a skill's body has degenerated into a pile
of disjoint session notes, restructure it with edit instead of appending
yet another blob.

User-preference embedding (important): when the user expressed a style /
format / workflow preference, the update belongs in the SKILL.md body,
not just in memory. Memory captures "who the user is"; skills capture
"how to do this class of task for this user". When they complain about
how you handled a task, the skill that governs that task needs to carry
the lesson.

Do NOT capture (these become persistent self-imposed constraints that
bite you later when the environment changes):
  • Environment-dependent failures: missing binaries, install errors,
    post-migration path mismatches, "command not found", unconfigured
    credentials, uninstalled packages. The user can fix these — they are
    not durable rules.
  • Negative claims about tools or features ("X tool is broken", "cannot
    use Y from the sandbox"). These harden into refusals the agent cites
    against itself for months after the problem was fixed.
  • Session-specific transient errors that resolved before the
    conversation ended. If retrying worked, the lesson is the retry
    pattern, not the original failure.
  • One-off task narratives. "Summarize today's market" or "analyze this
    PR" is not a class of work that warrants a skill.

If a tool failed because of setup state, capture the FIX (install command,
config step, env var) under an existing setup or troubleshooting skill —
never "this tool does not work" as a standalone constraint.

"Nothing to save." is a real option but should NOT be the default. If the
session ran smoothly with no corrections and produced no new technique,
just say "Nothing to save." and stop. Otherwise, act."""
