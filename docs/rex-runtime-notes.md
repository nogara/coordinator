# Rex runtime facts (spike, 2026-11-27)

Observed with the `rex` CLI inside a Rex terminal. Claude Code 2.1.332 in the spike block.

## Starting the agent
```
rex window new spike -c /Users/nogara/.dotfiles --focus=false --shell none --json -- zsh -lic 'exec claude --allowedTools "Bash(sleep:*)"'
```
Exit 0, stdout JSON: `{"window_id":"window:...","block_ids":["block:..."],"revision":N}`.
Claude took about 6 s to show its prompt. No trust prompt appeared (cwd already trusted).

`-s` accepts a full block ID wherever a session is expected; Rex resolves it to the containing session. Verified: `rex api call -s "$REX_BLOCK" session.view '{}'` returns the containing session (`"session_id":"session:...","label":"dawn-lagoon"`). A nonexistent block id fails with `Error: session target "block:0000..." not found: no open block has ID "block:0000..."`. A session-less method such as `target.resolve` with `{}` fails with `target requests require at least one kind (status=400 code=bad_request)`, which is unrelated to resolution. Verified form for RexTerminals: `rex api call -s <block_id> block.event -` (block_id may be used as `-s`).

Prompt delivery (both exit 0, the api call prints `{}`):
```
echo '{"block_id":"<id>","event":{"type":"paste","payload":{"text":"..."}}}' | rex api call -s "$REX_BLOCK" block.event -
sleep 0.5; rex send-key -b <id> enter
```

## Step 2: `rex block call -b <id> program_status '{}'` with a background shell running
Claude started `sleep 120` in the background and ended its turn. Output (one line, stable across polls):
```json
{"records":[
 {"state":"working","app":"claude-code","effective_app":"claude-code","msg":"1 shell still running","updated_at":"2026-11-27T09:44:23.449277-03:00","owner":{"pid":21977,"name":"2.1.332"}},
 {"id":"bd3s557nd","state":"working","app":"claude-code","effective_app":"claude-code","title":"Wait 120 seconds in background","updated_at":"...","owner":{"pid":21977,"name":"2.1.332"}}
]}
```
- Root record (no `id`) has `msg` = `"1 shell still running"` (count-style message): YES.
- Per-task record with an `id` (the background shell's task id, same one Claude prints) and a `title`: YES.
- `state` is `working` for both while the shell runs, even though the turn ended.
- Conclusion: `still_running` can work on Rex; `RexTerminals.get` can fill `records` from the records with an `id`.
  The "no records" fallback line (`test_still_running_is_empty_without_records`) is still worth keeping as a cheap test but is not forced by this spike.

## Other block calls
`rex block call -b <id> process '{}'` (live Claude block; this full example is from the coordinator's own block, run via a `login`/`zsh` shell, so it has `ancestors`; the throwaway spike block was started with `--shell none` and had no `ancestors`, only `child`, `foreground`, `home`):
```json
{"last_exit":null,"child":{"pid":42678,"user_id":0,"user":"root","name":"login","path":"/usr/bin/login"},"foreground":{"pid":43030,"user_id":502,"user":"nogara","name":"2.1.332","path":"/Users/nogara/.local/share/claude/versions/2.1.332","invoked_path":"/Users/nogara/.local/bin/claude","argv0":"claude","cwd":"/Users/nogara/.dotfiles"},"ancestors":[{"pid":42679,"user_id":502,"user":"nogara","name":"zsh","path":"/opt/homebrew/Cellar/zsh/5.9.2/bin/zsh","invoked_path":"/opt/homebrew/bin/zsh","argv0":"-zsh","cwd":"/Users/nogara/.dotfiles"},{"pid":42678,"user_id":0,"user":"root","name":"login","path":"/usr/bin/login"}],"home":"/Users/nogara"}
```
`rex block inspect <id> --json` on a live block: top-level keys `session_id`, `block`, `api`.
`session_id` is the full id form, e.g. `session:01a21a96-35cf-736b-9c25-0f335f6c0f1c`.

## Step 3: gone signal (after `rex window close <window_id>`, which exits 0)
`rex block inspect <closed id> --json`:
- exit code **3**
- stdout empty
- stderr: `Error: block target "block:<uuid>" not found`

The same exit 3 and message come from `rex block call -b <id> process '{}'` and `rex capture -b <id> --trim`.
(Other errors use other codes, e.g. exit 2 for a usage error, so test for 3 and/or the `not found` text.)

## Step 4: `rex block ls -A --json` with two sessions
- It does NOT work: exit **2**, stderr `Error: rex block ls --json requires one session. Pass -s SESSION`
  (followed by usage text). Adding `-s` together with `-A` gives the same error.
- `rex block ls -s <session_id> --json` works:
  ```json
  {"blocks":[{"block_id":"block:...","label":"shell","creator_name":"com.superlogical.terminal","flavor":"com.superlogical.terminal.shell","host_id":"<uuid>","placement":"placed","window_id":"window:..."}]}
  ```
  No session id field per block. (`label`, `window_id`, `block_id` only; the session is implied by `-s`.)
- `rex block ls -A` (table, no `--json`) is a human-readable table, not for parsing; it lists blocks of every session, columns `SESSION WINDOW ID FLAVOR LABEL PLACEMENT OWNER`; SESSION is the session's name, ID/OWNER are short ids (suffix of the uuid).
- `rex block ls -A -q` prints full `block:<uuid>` ids, one per line, across all sessions (no session info).
- Consequence for Task 3: there is no cross-session JSON listing and no session id per block, so keep the per-block `rex block inspect <id> --json` (`session_id`) to learn a block's session, or list per session with `-s`.

## Cleanup
Spike window closed with `rex window close window:...` (exit 0). Temporary session `spike2` created with `rex new spike2 --json -c /tmp -- zsh` (JSON: `session_id`, `initial_windows[{window_id,block_ids,revision}]`) and removed with `rex kill session:<id>` (exit 0).
