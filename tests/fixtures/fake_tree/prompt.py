"""Fake system prompt: three markdown sections joined as-is."""

IDENTITY = "You are a synthetic test agent used by hermes-parity.\n\n"

TASK_GUIDANCE = (
    "# Finishing the job\n"
    "Report what real execution returned.\n"
    "\n"
)

TOOLS_GUIDANCE = (
    "# Tool use\n"
    "Prefer the file tools for reading.\n"
)


def build_system_prompt(now):
    return IDENTITY + TASK_GUIDANCE + TOOLS_GUIDANCE + f"# Clock\nConversation started: {now.isoformat()}\n"
