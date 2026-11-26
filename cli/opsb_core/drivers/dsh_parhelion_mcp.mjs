// Parhelion contest tools for DeepSeek Harness Desktop, mounted per Agent.
// Installed into the DSH profile by opsb (dsh_driver.ensure_parhelion_mcp_plugin); do not
// edit the copy under ~/.dsh, it is overwritten on the next launch.
//
// An Agent whose working directory sits inside a Parhelion contest workspace (a folder
// holding opsb.workspace.json, possibly a worker worktree below it) gets the orchestrator
// MCP server for that contest root. Other workspaces are left untouched.
import { appendFileSync, existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

export const name = 'global-parhelion-mcp'
export const inject = ['agents', 'loader']

function log(event) {
  try {
    appendFileSync(join(homedir(), '.dsh', 'parhelion-mcp.log'),
      JSON.stringify({ time: new Date().toISOString(), ...event }) + '\n',
      { mode: 0o600 })
  } catch (_) {}
}

function contestRoot(start) {
  let dir = start
  for (let i = 0; i < 12 && dir; i += 1) {
    if (existsSync(join(dir, 'opsb.workspace.json'))) return dir
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return null
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
    const cwd = agent.session.header.cwd || homedir()
    const root = contestRoot(cwd)
    if (!root) return
    let fiber
    try {
      fiber = agent.ctx.plugin(mcpClient, {
        serverName: 'parhelion',
        transport: 'stdio',
        command: config.command || 'python3',
        args: [config.opsb || join(homedir(), '.local', 'bin', 'opsb'), 'mcp', 'serve', '--workspace', root],
        cwd: root,
        // parhelion_agent_wait and parhelion_test_wait block for up to 45 s.
        toolCallTimeoutMs: 120000,
        failOnStartupError: false
      })
      fibers.set(agent.id, fiber)
      agent.ctx.effect(() => () => {
        if (fibers.get(agent.id) === fiber) fibers.delete(agent.id)
      }, 'global-parhelion-mcp.agent')
      await fiber
      log({ event: 'mounted', agent: String(agent.id), cwd, root })
    } catch (error) {
      if (fiber) await fiber.dispose().catch(() => {})
      fibers.delete(agent.id)
      log({ event: 'failed', agent: String(agent.id), cwd, root, error: String(error) })
      ctx.logger.warn(`parhelion MCP could not start for ${root}: ${String(error)}`)
    }
  }
  ctx.on('agent/created', ({ agent }) => mount(agent))
  await Promise.all(ctx.agents.list().map(mount))
}
