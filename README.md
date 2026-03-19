# PM Agent Swarm — Mission Control

An AI-powered multi-agent orchestration platform for product management workflows. Six specialized agents collaborate to produce customer discovery insights, competitive analysis, prioritized roadmaps, PRDs, validation reports, and executive summaries — with human-in-the-loop checkpoints at every stage.

Built as part of MKTG 454 Ad Hoc Honors Project at UW Foster School of Business.

## Prototype Scope

This project is a local prototype/proof of concept intended to be run on the end user's machine.

- Run on localhost only (do not expose directly to the public internet).
- Avoid using sensitive/regulated data in uploaded files or prompts.
- If a run gets stuck after accidental refresh, use **Reset Session** in the header.

## Features

- **6 Specialized Agents**: Discovery, Competitive Analyst, Roadmap, PRD Writer, Validator, and Synthesis
- **Parallel Execution**: Discovery and Competitive agents run simultaneously
- **Human-in-the-Loop Checkpoints**: Approve or reject each agent's output individually with feedback-driven revision loops
- **3 Running Modes**: Mock (demo), Local (Ollama), and Live (Anthropic API)
- **Real-time Streaming**: Watch agent outputs stream in real-time with markdown rendering
- **Context Input**: Paste interview transcripts, upload files, and provide competitive notes
- **Export**: Copy individual outputs or download all agent deliverables as markdown
- **Session Persistence + Recovery**: Pipeline state survives page refreshes, and a **Reset Session** button clears stale mid-run state after accidental refreshes
- **Expand View**: Full-screen modal for reading agent outputs comfortably
- **Implementation Brief**: One-click generation of an engineering handoff document from the PRD, ready for Claude Code or Cursor

## Quick Start

### Prerequisites

- Python 3.8+
- pip

### Install

```bash
cd prototype
pip install -r requirements.txt
```

### Run in Demo Mode (no API key needed)

```bash
python server.py
```

Open http://localhost:8000 in your browser.

If a run gets stuck after an accidental refresh, click **Reset Session** in the top-right header to clear saved session data and return to a clean state.

### Run with Local AI (free, via Ollama)

```bash
# Install Ollama: https://ollama.com
ollama pull qwen3.5:9b

# Set model and start (must match model pulled in Ollama)
export LOCAL_MODEL=qwen3.5:9b
python server.py --local
```

You can also set the local model in a `.env` file instead of exporting it in your shell:

```bash
cp .env.example .env
# Edit .env
LOCAL_MODEL=qwen3.5:9b
python server.py --local
```

### Run with Anthropic API (highest quality)

```bash
# Copy and configure .env
cp .env.example .env

# Edit .env and add your ANTHROPIC_API_KEY
python server.py --live
```

## Configuration

All configuration is via environment variables or `.env` file:

| Variable            | Default             | Description                                            |
| ------------------- | ------------------- | ------------------------------------------------------ |
| `ANTHROPIC_API_KEY` | —                   | Required for `--live` mode                             |
| `ANTHROPIC_MODEL`   | `claude-sonnet-4-6` | Model for live mode                                    |
| `LOCAL_MODEL`       | `qwen3.5:9b`        | Model for `--local` mode (must be pulled in Ollama)    |
| `MAX_UPLOAD_BYTES`  | `1048576`           | Max upload size in bytes for `/upload` (default: 1 MB) |
| `PORT`              | `8000`              | Server port                                            |

## Architecture

```
Browser (React SPA)
    ↕ WebSocket
Tornado Server (server.py)
    ↕
Agent Executors
    ├── MockAgentExecutor (demo mode)
    ├── LiveAgentExecutor (Anthropic API)
    └── LocalAgentExecutor (Ollama)
```

### Pipeline Flow

1. **Orchestrator** broadcasts pipeline strategy
2. **Discovery + Competitive** run in parallel → Checkpoint 1
3. **Roadmap Agent** prioritizes features → Checkpoint 2
4. **PRD Writer** drafts requirements → Checkpoint 3
5. **Validator** audits all outputs for accuracy
6. **Synthesis** produces executive summary

At each checkpoint, the PM reviews outputs and can approve or reject individual agents with feedback.

## Project Structure

```
prototype/
├── server.py              # Tornado backend (all 3 executors + WebSocket + API)
├── static/
│   └── index.html         # React SPA (single-file, CDN dependencies)
├── test_data/             # Sample PM research data for testing
│   ├── product_description.txt
│   ├── interview_data.txt
│   └── competitive_notes.txt
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
├── LICENSE                # MIT license
├── .gitignore
└── README.md
```

## Test Data

Sample PM research data is included in `test_data/` for a fictional product called "FocusFlow" (an AI-powered deep work assistant). Paste the contents into the context input fields to test with realistic data.

## Technology Stack

- **Backend**: Python 3, Tornado (WebSocket + HTTP)
- **Frontend**: React 18, Babel standalone, marked.js, DOMPurify (all via CDN)
- **AI**: Anthropic Claude API or Ollama (OpenAI-compatible)
- **Persistence**: JSON file-based session state

## License

MIT License. See LICENSE for details.

## Author

Adam Ketefian — [LinkedIn](https://www.linkedin.com/in/adamketefian)
