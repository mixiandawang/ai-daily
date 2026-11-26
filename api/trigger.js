// Vercel Cron -> 调 GitHub Actions 的 workflow_dispatch（替代不可靠的 GitHub schedule）
// 环境变量：GH_TOKEN = 细粒度 PAT（仅 ai-daily，Actions: Read and write）
module.exports = async function handler(req, res) {
  const token = process.env.GH_TOKEN;
  if (!token) {
    res.status(500).json({ ok: false, error: "缺少环境变量 GH_TOKEN" });
    return;
  }
  const r = await fetch(
    "https://api.github.com/repos/mixiandawang/ai-daily/actions/workflows/daily.yml/dispatches",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-daily-vercel-cron",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );
  const body = (await r.text()).slice(0, 300);
  res.status(r.ok ? 200 : r.status).json({ ok: r.ok, github_status: r.status, body });
};
