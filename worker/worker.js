/**
 * Cloudflare Worker: live Q&A backend for the Onsale Page View Comparison Dashboard.
 *
 * Why this exists: the dashboard is a public static site (GitHub Pages), so it can't hold an
 * Anthropic API key client-side. This Worker holds the key as a secret, fetches the dashboard's
 * already-published data straight from GitHub Pages (no separate data store to keep in sync),
 * and answers free-text questions from the user with a real Claude call.
 *
 * Data scope: this answers questions using the ALREADY-EXTRACTED structured data
 * (onsales.json / venue-baselines.json / insights.json) rather than re-reading raw Google Drive
 * sheets directly -- giving this Worker its own Drive access would need a Google Cloud service
 * account, which is blocked by IT policy on this account (see main README). The structured data
 * is refreshed hourly by the scheduled sync task, so it's a close proxy for "the reports."
 *
 * Endpoints:
 *   POST /ask   { question: string }  ->  { answer: {...}, memory_updated: boolean }
 *   OPTIONS *   CORS preflight
 *
 * Required Worker secrets/vars (set via `wrangler secret put` or the Cloudflare dashboard):
 *   ANTHROPIC_API_KEY   - Claude API key
 *   SITE_KEY            - a shared string the frontend also sends; NOT a real secret (it ships
 *                          in public JS) but stops casual/automated scraping of this endpoint
 *                          from consuming your Claude API quota now that the repo is public.
 *   DASHBOARD_ORIGIN    - scheme+host only, e.g. "https://charlieogbechie.github.io" (used for
 *                          the CORS allow-origin header -- must not include a path)
 *   DATA_BASE_URL       - full base URL to the published data files, e.g.
 *                          "https://charlieogbechie.github.io/onsale-pageview-dashboard"
 *
 * Optional KV binding (see wrangler.toml):
 *   BENCHMARK_MEMORY    - Workers KV namespace used to remember established comparison sets
 *                          across questions (e.g. "for Tour X we compared against venues A/B/C").
 *                          If not bound, the Worker just skips memory recall/update gracefully.
 */

const CLAUDE_MODEL = "claude-sonnet-4-5";
const MEMORY_KEY = "benchmark_memory_v1";
const MAX_MEMORY_ENTRIES = 50;

const SYSTEM_PROMPT = `You are a demand analyst answering questions about Live Nation onsale page-view and \
queue data for a ticketing company's tour team, via a dashboard's "Ask a question" box. You are \
given the current normalized dataset (onsales.json), per-venue benchmark bands \
(venue-baselines.json), previously-generated commentary (insights.json), and a small amount of \
"memory" of benchmark comparisons already established in earlier questions, if any.

═══ CORE WORKFLOWS ═══

When the user asks for insights from the data:
- Identify the most relevant records for the question.
- Use enough of them to answer confidently.
- Synthesize the key findings instead of restating records.
- Call out notable changes, patterns, anomalies, risks, and standout demand signals.
- Compare across reports/shows when that materially improves understanding.

When the user asks about a specific report/show/tour:
- Focus on that one first.
- Summarize the most important takeaways, metrics, and context.
- Explain what the numbers imply about likely demand.
- Note anything unusually strong, weak, changed, missing, or inconsistent.

When the user asks what changed:
- Default to latest vs. prior comparable data point unless the user specifies a different comparison set.
- Explain the most important differences first.
- Include why those differences matter.

When the user asks for benchmark or peer comparisons for a tour:
- Start with the SAME VENUE wherever possible (this is the strongest basis: it holds city, capacity, and local market constant).
- Build a small sample of the most relevant comparison events. Only fall back to artist/genre/audience/market similarity when same-venue history is too thin -- and say so explicitly when you do.
- Explain why those events are the right comparison set.
- Compare pre-presale attention and queue behavior separately, then together.
- Conclude with a plain-English assessment: does this tour appear weaker, in line, or stronger than the comparison set?
- Never imply a trend from a single data point -- distinguish a one-report observation, a small-sample comparison (2-4 events), and a stronger multi-event pattern (5+ events).

═══ DEFAULT ANSWER GUIDE ═══

Default to concise, insight-first answers. Return ONLY a JSON object (no prose outside it, no markdown fences) with this shape:
{
  "key_insights": [string, ...],
  "comparison_basis": string,
  "trends_or_changes": [string, ...],
  "risks_or_gaps": [string, ...],
  "demand_readout": string,
  "reports_used": [string, ...],
  "memory_update": { "key": string, "comparison_basis": string, "peers": [string] } | null
}

Rules for filling this in:
- key_insights: the clearest takeaways FIRST, not a recap of the data. 2-5 bullet-style strings.
- comparison_basis: one or two sentences naming which shows/venues/reports you used as the comparison set and why (venue match first, then sales stage/time window/report type, then artist/genre/market similarity as a fallback -- state which tier you used).
- trends_or_changes: direction + magnitude + why it matters, empty array if not applicable to this question.
- risks_or_gaps: contradictions, missing context, small sample sizes, non-comparable systems (e.g. FR/IT queue systems), anomalies flagged in the data -- empty array if genuinely none.
- demand_readout: one clear plain-language sentence translating the numbers into a demand assessment. Make clear whether this is a strong or tentative conclusion.
- reports_used: the specific artist/venue/source_file values you actually drew on.
- memory_update: ONLY populate this if you established a genuinely new or updated benchmark comparison set worth remembering for future questions (e.g. "for Tour X, the right peer set is A, B, C at similar venues"). Otherwise null. Keep it compact.

If prior "memory" is provided and relevant to this question, use it for consistency rather than re-deriving a different comparison set from scratch -- but if the current data contradicts it, prefer the current data and update memory accordingly.

You may note when you'd want outside/web context to pick better comparison peers, but you have no web access here -- work only from the provided data and say so if that's a real limitation for this question.`;

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin || "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Site-Key",
    "Content-Type": "application/json",
  };
}

async function fetchJson(url) {
  const res = await fetch(url, { cf: { cacheTtl: 60 } });
  if (!res.ok) throw new Error(`Failed to fetch ${url}: ${res.status}`);
  return res.json();
}

async function readMemory(env) {
  if (!env.BENCHMARK_MEMORY) return [];
  try {
    const raw = await env.BENCHMARK_MEMORY.get(MEMORY_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch (e) {
    return [];
  }
}

async function writeMemory(env, entries) {
  if (!env.BENCHMARK_MEMORY) return;
  try {
    await env.BENCHMARK_MEMORY.put(MEMORY_KEY, JSON.stringify(entries.slice(-MAX_MEMORY_ENTRIES)));
  } catch (e) {
    // Memory is a nice-to-have; never fail the request over it.
  }
}

function relevantMemory(memory, question) {
  const q = question.toLowerCase();
  return memory.filter(m => q.includes((m.key || "").toLowerCase().split(" ")[0] || "\0"));
}

export default {
  async fetch(request, env) {
    const origin = env.DASHBOARD_ORIGIN || "*";

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders(origin) });
    }

    const url = new URL(request.url);
    if (url.pathname !== "/ask" || request.method !== "POST") {
      return new Response(JSON.stringify({ error: "Not found" }), {
        status: 404,
        headers: corsHeaders(origin),
      });
    }

    // Basic scraping deterrent -- not real security (this ships in public JS), just raises
    // the bar above "anyone who finds the URL by accident" now that the repo is public.
    if (env.SITE_KEY && request.headers.get("X-Site-Key") !== env.SITE_KEY) {
      return new Response(JSON.stringify({ error: "Unauthorized" }), {
        status: 401,
        headers: corsHeaders(origin),
      });
    }

    let body;
    try {
      body = await request.json();
    } catch (e) {
      return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
        status: 400,
        headers: corsHeaders(origin),
      });
    }

    const question = (body.question || "").trim();
    if (!question) {
      return new Response(JSON.stringify({ error: "Missing 'question'" }), {
        status: 400,
        headers: corsHeaders(origin),
      });
    }
    if (question.length > 2000) {
      return new Response(JSON.stringify({ error: "Question too long" }), {
        status: 400,
        headers: corsHeaders(origin),
      });
    }

    const base = env.DATA_BASE_URL || "https://charlieogbechie.github.io/onsale-pageview-dashboard";

    let onsales, baselines, insights;
    try {
      [onsales, baselines, insights] = await Promise.all([
        fetchJson(`${base}/data/onsales.json`),
        fetchJson(`${base}/data/venue-baselines.json`),
        fetchJson(`${base}/data/insights.json`).catch(() => null),
      ]);
    } catch (e) {
      return new Response(JSON.stringify({ error: `Could not load dashboard data: ${e.message}` }), {
        status: 502,
        headers: corsHeaders(origin),
      });
    }

    const memory = await readMemory(env);
    const usefulMemory = relevantMemory(memory, question);

    const payload = {
      question,
      onsales,
      venue_baselines: baselines,
      prior_insights: insights,
      relevant_memory: usefulMemory,
    };

    let claudeRes;
    try {
      claudeRes = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": env.ANTHROPIC_API_KEY,
          "anthropic-version": "2023-06-01",
        },
        body: JSON.stringify({
          model: CLAUDE_MODEL,
          max_tokens: 2000,
          system: SYSTEM_PROMPT,
          messages: [{ role: "user", content: JSON.stringify(payload).slice(0, 180000) }],
        }),
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: `Claude API request failed: ${e.message}` }), {
        status: 502,
        headers: corsHeaders(origin),
      });
    }

    if (!claudeRes.ok) {
      const errText = await claudeRes.text();
      return new Response(JSON.stringify({ error: `Claude API error: ${claudeRes.status} ${errText}` }), {
        status: 502,
        headers: corsHeaders(origin),
      });
    }

    const claudeData = await claudeRes.json();
    const rawText = (claudeData.content && claudeData.content[0] && claudeData.content[0].text) || "";
    const cleaned = rawText.trim().replace(/^```(json)?/i, "").replace(/```$/, "").trim();

    let answer;
    try {
      answer = JSON.parse(cleaned);
    } catch (e) {
      // Fall back to a minimal shape so the frontend always has something sensible to render.
      answer = {
        key_insights: [rawText || "Claude returned an unparseable response."],
        comparison_basis: "",
        trends_or_changes: [],
        risks_or_gaps: ["Response could not be parsed as structured JSON."],
        demand_readout: "",
        reports_used: [],
        memory_update: null,
      };
    }

    let memoryUpdated = false;
    if (answer.memory_update && answer.memory_update.key) {
      const next = memory.filter(m => m.key !== answer.memory_update.key);
      next.push({ ...answer.memory_update, updated_at: new Date().toISOString() });
      await writeMemory(env, next);
      memoryUpdated = true;
    }
    delete answer.memory_update;

    return new Response(JSON.stringify({ answer, memory_updated: memoryUpdated }), {
      headers: corsHeaders(origin),
    });
  },
};
