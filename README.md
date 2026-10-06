# Consumer Trend & Social Signal Summarization Agent

AI agent for summarizing consumer trends from social and demand signals, built with Agentic Star.

> **Category**: Cat 2 (domain-specific multi-step pipeline)
> **Industry**: Retail
> **Template ID**: RET-C2-332

## Overview

Turns raw consumer-demand signals into a ranked, ready-to-read trend briefing for a retail
category. A caller submits signals from whatever sources it already has — social posts, search
interest, sales velocity, competitor movement — each as a keyword with a weight. A deterministic
five-step pipeline validates the request, merges the sources and removes duplicate keywords while
recording which sources each one came from, scores every keyword by weight and source reliability
with a boost for keywords corroborated across more than one source, and writes a structured
summary: a headline, the top-ranked trends, a four-week demand outlook, recommended merchandising
actions, and a breakdown of which social platforms carried the signal. It is aimed at merchandising
and category teams who otherwise assemble this by hand every week.

Two properties are worth knowing before you adapt it. First, **competitor identity never survives
the boundary**: a competitor row is reduced to its category-level intent as it is validated, and the
output gate independently refuses to publish anything in which a blocked identity appears — so the
reduction cannot be bypassed by a later change. Second, **the demand outlook always ships with its
advisory disclaimer**; the output gate treats a missing disclaimer as a refusal, because a forecast
that loses its caveat reads as a guarantee.

No model is invoked anywhere in the pipeline. Ranking and summary generation are deterministic, so
the same signals always produce the same briefing — which is what makes the output reviewable, and
what makes the disclaimer meaningful.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent imports its base classes from the framework package at start-up, so without that
package installed and configured, import and graph compile fail outright rather than leaving the
agent running in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Calling the agent

`POST /invoke` takes the request line in `input` and the signals in `input_context.trend_request`:

```json
{
  "input": "Summarise consumer trend signals for electronics over the last four weeks.",
  "input_context": {
    "trend_request": {
      "category": "electronics",
      "period": "4 weeks",
      "social_signals": [{"keyword": "wireless earbuds", "weight": 3.0, "platform": "Instagram"}],
      "search_trends": [{"keyword": "wireless earbuds", "weight": 4.0}],
      "sales_velocity": [{"keyword": "wireless earbuds", "weight": 5.0, "channel": "offline"}],
      "competitor_activity": [{"category": "electronics", "intent": "price_drop", "weight": 1.0}]
    }
  }
}
```

`category` must be one of the recognised retail categories and `period` must be a week/day/month
count or a date range; a request outside those is answered as out of scope rather than as an error.
Every weight is parsed as a finite, bounded number and every keyword is held to an explicit
character class, so a malformed value is refused by name — the rejected value itself is never echoed
back. Sending the same request object as a JSON string in `input` also works, for callers that have
only that one field.

The same request, in the form the deployment smoke check posts, is committed at
`deploy/invoke_payload.json` and is generated from the boundary test suite's own fixture.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design notes and test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Widen the recognised category set and the blocked-identity list in `src/nodes/` for your market.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
