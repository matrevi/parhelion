// Parhelion contest tools for DeepSeek Harness Desktop, mounted per Agent.
// Installed into the DSH profile by opsb (dsh_driver.ensure_parhelion_mcp_plugin); do not
// edit the copy under ~/.dsh, it is overwritten on the next launch.
//
// An Agent whose working directory sits inside a Parhelion contest workspace (a folder
// holding opsb.workspace.json, possibly a worker worktree below it) gets an MCP server for
// that contest root, scoped to the Agent's role:
//
//   - the main conversation at the contest root is the orchestrator;
//   - a conversation under `.worktrees/*`, and every native subagent, is a worker;
//   - anything the plugin cannot classify is a worker (least privilege: `opsb mcp serve
//     --role worker` hides the orchestrator-only tools instead of granting them).
//
// On-demand mount (4.1 #15): `@deepseek-ai/dsh-mcp-client` has no lazy mode — `apply()` opens
// the transport, spawns the stdio child and awaits the first tool sync before activation
// (lib/index.js: `const outcome = await connection.ready`), and its config schema has no
// startup/idle field. Deferring `ctx.plugin(...)` instead is unsound here: the agent loop
// assembles `assembly.tools` *before* the `agent/pre-step` waterfall (dsh-agent-loop/lib/index.js,
// `preStep()`), so a mount started at the first step would be missing from that step's tool
// list — exactly the step in which a fresh worker calls the platform for the first time. The
// mount therefore stays eager and per Agent, and the process it owns ends when the Agent does.
import { appendFileSync, existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, relative, sep } from 'node:path'

export const name = 'global-parhelion-mcp'
export const inject = ['agents', 'loader']

const WORKTREE_DIR = '.worktrees'
const ROLES = ['orchestrator', 'worker']

function log(event) {
  try {
    appendFileSync(join(homedir(), '.dsh', 'parhelion-mcp.log'),
      JSON.stringify({ time: new Date().toISOString(), ...event }) + '\n',
      { mode: 0o600 })
  } catch (_) {}
}

export function contestRoot(start) {
  let dir = start
  for (let i = 0; i < 12 && dir; i += 1) {
    if (existsSync(join(dir, 'opsb.workspace.json'))) return dir
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return null
}

// Role from the two facts the plugin context exposes: the session cwd and the session header.
// `origin: 'subagent'` and `parentSession` are written by @deepseek-ai/dsh-subagent for every
// native child (childSessionMeta), so a child never inherits the orchestrator role of its parent.
export function agentRole(cwd, header, root) {
  const meta = header && typeof header === 'object' ? header : {}
  if (meta.origin === 'subagent' || meta.parentSession) return 'worker'
  const dir = cwd || ''
  if (!root || !dir) return 'worker'
  const rel = relative(root, dir)
  if (rel === '') return 'orchestrator'
  if (rel === '..' || rel.startsWith(`..${sep}`)) return 'worker'
  if (rel.split(sep).includes(WORKTREE_DIR)) return 'worker'
  return 'worker' // an unclassified folder inside the contest: least privilege
}

// The server configuration for one Agent, or null when this Agent gets no Parhelion server.
export function planMount(agent, config = {}) {
  const header = (agent && agent.session && agent.session.header) || {}
  const cwd = header.cwd || homedir()
  const root = contestRoot(cwd)
  if (!root) return { root: null, role: null, mount: null }
  const role = agentRole(cwd, header, root)
  if (!ROLES.includes(role)) return { root, role: 'worker', mount: null }
  const opsb = config.opsb || join(homedir(), '.local', 'bin', 'opsb')
  return {
    root,
    role,
    mount: {
      serverName: 'parhelion',
      transport: 'stdio',
      command: config.command || 'python3',
      args: [opsb, 'mcp', 'serve', '--workspace', root, '--role', role],
      cwd: root,
      // parhelion_agent_wait and parhelion_test_wait block for up to 45 s.
      toolCallTimeoutMs: 120000,
      failOnStartupError: false
    }
  }
}

export async function apply(ctx, config = {}) {
  const fibers = new Map()
  let disposed = false
  ctx.effect(() => async () => {
    disposed = true
    const results = await Promise.allSettled([...fibers.values()].map(fiber => fiber.dispose()))
    fibers.clear()
    for (const result of results) {
      if (result.status === 'rejected') ctx.logger.warn(String(result.reason))
    }
  }, 'global-parhelion-mcp.clients')

  const mcpClient = await ctx.loader.import('@deepseek-ai/dsh-mcp-client', () => name)
  if (disposed) return
  const mount = async agent => {
    if (disposed || fibers.has(agent.id)) return
    const plan = planMount(agent, config)
    if (!plan.mount) return
    const { root, role, mount: server } = plan
    let fiber
    try {
      fiber = agent.ctx.plugin(mcpClient, server)
      fibers.set(agent.id, fiber)
      agent.ctx.effect(() => () => {
        if (fibers.get(agent.id) === fiber) fibers.delete(agent.id)
      }, 'global-parhelion-mcp.agent')
      await fiber
      log({ event: 'mounted', agent: String(agent.id), role, root })
    } catch (error) {
      if (fiber) await fiber.dispose().catch(() => {})
      fibers.delete(agent.id)
      log({ event: 'failed', agent: String(agent.id), role, root, error: String(error) })
      ctx.logger.warn(`parhelion MCP could not start for ${root}: ${String(error)}`)
    }
  }
  ctx.on('agent/created', ({ agent }) => mount(agent))
  await Promise.all(ctx.agents.list().map(mount))
}
