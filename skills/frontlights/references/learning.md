# Learning mode

Helps a user who is still learning to understand the technical decisions of the
grilling while they make them. Read this file before writing the first
explanation: when the user turns learning mode on, or when they ask for an
explanation while it is off. Everything the user reads is in the user's language;
these instructions are not.

## What every explanation must have

1. The concept in plain words. Define every term the user may not have met.
2. Why this project does it this way. Cite the file and line where the
   convention shows up (facts) and keep your own recommendation apart, labelled
   as an opinion.
3. A short excerpt from the project's own code, at most about fifteen lines,
   with its path and a comment on each relevant line. Never include secrets,
   credentials or other people's data; when the only example has them, describe
   it instead of quoting it.
4. What each option on the table would change, in one or two sentences each.

Write the explanation as text in the conversation, before the question, never
inside an option's `preview`: it must stay readable later. The `preview` of the
"Explicar antes de decidir" option only lists what will be explained.

## When to explain

Explain only before technical decisions: the solution round, and the decision
tree branches about architecture, libraries, data, tests or security. Scope,
priorities and other product questions get no explanation and no explain option,
unless the user asks for one in free text.

- Learning mode on: write the explanation before the question. No slot is spent
  on an explain option, so a technical question may carry up to four real
  options.
- Learning mode off: make the last option of each technical question
  "Explicar antes de decidir". It takes one slot, so the question then has at
  most three real options, and the solution round has exactly three approaches.
  When the user picks it, or asks in free text, explain and then ask the same
  question again together with the understanding check below.
- Never treat an explanation as a decision. The user still chooses, and "Entendi"
  is never permission to decide for them.

The option labels in this file are in Portuguese; translate them when the user
writes in another language.

## Understanding check

After the explanations of a round, ask one "Ficou claro?" covering every concept
explained in the round. Put it in the same `AskUserQuestion` call as the pending
decisions: one check plus up to three decisions, because a call holds at most
four questions. Its options:

- "Entendi": the decisions in the same call stand.
- "Explicar de outro jeito": re-explain from a different angle (an analogy, a
  smaller step, another excerpt), then ask again. There is no limit on
  re-explanations. The decisions answered in the same call are not final: ask
  them again after the new explanation.
- From the second re-explanation on, add "Seguir com a recomendação e marcar a
  revisar": take the recommended option for the pending decisions and list each
  one under "Open decisions" as to be reviewed, so the user can come back to it.

## Record

In `.frontlights/discovery.md`, record the learning mode (on or off) and add one
row per concept explained to the "Concepts explained" table: its name, a
one-line summary and where it came up in the grilling. Re-explaining a concept
adds no row. Never record secrets, credentials or other people's data.
