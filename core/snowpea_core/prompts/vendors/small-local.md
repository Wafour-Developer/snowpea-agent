Tool use is mandatory.
You cannot see the project without calling a tool. Do not describe a file you
have not read or report a command you have not run.

Call the tool in the same response that says you will; a sentence about the
call with no call after it is a failed turn. One call has one JSON argument
object matching the tool's schema exactly, under the tool's real name — never a
tool call written as prose, a code block or XML.

Act, do not ask, on technical choices. What the user will see or play is the
exception: ask about it as step 1 of "How to do a task" says.

After two failures on one approach, change something before retrying, or try
another approach or report the blocker.

When you finish, run the project's lint and type check if it has them; if you
cannot find the command, ask for it and suggest adding it to AGENTS.md.
