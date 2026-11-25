# hermes-parity report

OLD `caafe5389ae5` vs NEW `unknown`; modes A, B.
36 difference records in 3 fingerprints.

## Runs

| scenario | mode | old | new | steps old/new | API requests old/new | diffs |
|---|---|---|---|---|---|---|
| a_plain_chat | A | ok | ok | 1/1 | 1/1 | 3 |
| a_plain_chat | B | ok | ok | 4/4 | 1/1 | 3 |
| b_one_read_tool | A | ok | ok | 1/1 | 1/1 | 3 |
| b_one_read_tool | B | ok | ok | 9/9 | 2/2 | 6 |
| c_two_tools_sequence | A | ok | ok | 1/1 | 1/1 | 3 |
| c_two_tools_sequence | B | ok | ok | 14/14 | 3/3 | 9 |
| d_refused_tool | A | ok | ok | 1/1 | 1/1 | 3 |
| d_refused_tool | B | ok | ok | 7/7 | 2/2 | 6 |

## Fingerprints

| fingerprint | count | kind | piece | offset | length delta | modes | scenarios | new kind | summary |
|---|---|---|---|---|---|---|---|---|---|
| `82f4b7a1b96d` | 12 | api_request | `system_prompt.section[#]` | 2^8 | -1 | A,B | a_plain_chat, b_one_read_tool, c_two_tools_sequence, d_refused_tool | - | a system prompt section changed: -1 bytes from byte 368 (12x) |
| `8bff6caba322` | 12 | api_request | `tools[read_file].schema` | 2^9 | 0 | A,B | a_plain_chat, b_one_read_tool, c_two_tools_sequence, d_refused_tool | - | tool schema for read_file changed: same length from byte 783, same structure (reordered or edited in place) (12x) |
| `2917c15efca5` | 12 | api_request | `tools[search_files].schema` | 2^4 | -2^8 | A,B | a_plain_chat, b_one_read_tool, c_two_tools_sequence, d_refused_tool | - | tool schema for search_files changed: -357 bytes from byte 24, key paths +0/-1 (12x) |
