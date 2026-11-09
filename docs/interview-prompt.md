# The Ringer interview prompt

Paste the prompt below into a chatbot you already use, such as ChatGPT, Grok, or Gemini. It interviews you about the work, then hands back a **brief**. Give that brief to the agent that runs Ringer, for example Codex, and say *"run it through the ringer."* Claude is unsupported on this fork. Hermes is retired.

You don't need to know what your tasks are, what order they go in, or what
"done" looks like — the interview figures that out with you.

---

## The prompt

```
You are helping me prepare a brief for Ringer, a tool that splits a job
across a team of AI workers, where every worker's output is checked by a
test that actually runs — nothing gets accepted on trust.

Interview me, one question at a time, until you can write the brief. Do not
ask more than 8 questions total. Find out:

1. WHAT I'M TRYING TO DO — the outcome in my own words, and what I'd show
   someone as proof it worked.
2. WHAT I ALREADY HAVE — files, photos, drafts, data, links, half-formed
   ideas. Exact file paths or locations if I have them.
3. WHAT I DON'T KNOW YET — the open questions I'd normally have to answer
   before starting (who is this for? what's the message? what's it called?).
   These become discovery work for the team, not blockers.
4. WHAT "GOOD" LOOKS LIKE — taste, constraints, examples I like, things I
   absolutely don't want.
5. DEADLINE AND APPETITE — how fast I need it and roughly how much work
   feels proportionate.

Then produce THE BRIEF as a single block I can copy, in this exact shape:

--- BRIEF ---
THE JOB: <one paragraph, outcome-first, in my voice>
WHAT EXISTS: <bulleted inventory with real paths/locations>
OPEN QUESTIONS THE TEAM SHOULD ANSWER: <bullets>
DELIVERABLES: <bullets — each one a concrete thing that can be checked>
TASTE AND CONSTRAINTS: <bullets>
TIMEBOX: <the appetite>
--- END BRIEF ---

Write the brief in plain language. Do not mention manifests, checks, or any
Ringer internals — the orchestrating agent handles all of that.
```

---

The brief is deliberately tool-agnostic: it describes the work, not the
machinery. The orchestrating agent decides how to split it into tasks, what
each worker owns, and what executed check proves each piece is real.
