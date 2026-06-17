# CLAUDE.md

# Cora Infrastructure & AI Operations Environment

## Overview

This server is the primary orchestration and infrastructure environment for the Cora AI Assistant platform.

The environment is designed around a recursive multi-agent architecture using:
- Docker containers
- MCP servers
- n8n orchestration
- Local LLM inference through NVIDIA DGX Spark
- Agent-based routing and execution
- Modular AI services

Cora is the primary UI and orchestration layer.

The DGX Spark system is responsible for local model inference and GPU acceleration.

This Ubuntu Server environment acts as the:
- orchestration layer
- integration hub
- automation runtime
- container host
- reverse proxy gateway
- persistent infrastructure backbone

---

# Current Infrastructure

## Operating System

- Ubuntu Server 26
- Headless Linux environment
- Docker-based architecture
- Managed remotely through SSH

---

# Installed Core Components

## Docker

Purpose:
- Container runtime platform
- Isolates services into reusable environments
- Standard deployment mechanism for all infrastructure services

Used For:
- n8n
- MCP servers
- Portainer
- Nginx Proxy Manager
- PostgreSQL
- Redis
- Future Cora services

---

## Docker Compose Plugin

Purpose:
- Multi-container orchestration
- Infrastructure-as-code deployment model
- Defines services using docker-compose.yml files

---

## Nginx Proxy Manager (NPM)

Purpose:
- Reverse proxy management
- SSL certificate management
- Domain routing
- Public ingress layer

Ports:
- 80 -> HTTP
- 443 -> HTTPS
- 81 -> NPM Admin UI

---

## Portainer

Purpose:
- Docker management dashboard
- Visual container management
- Infrastructure monitoring

Admin URL:
https://SERVER_IP:9443

---

# Architecture Philosophy

Core principle:

Cora = User Intelligence Layer
n8n = Workflow Automation Layer
MCP Servers = Tool Access Layer
DGX Spark = Inference Layer
Docker = Infrastructure Layer

---

# High-Level System Architecture

User
  ↓
Cora Frontend/UI
  ↓
Cora API / Agent Harness
  ↓
Router Agent (ATLAS)
  ↓
Specialist Agents
  ├── FORGE
  ├── SCRIBE
  ├── PULSE
  ├── SIGNAL
  └── CHRONOS
        ↓
MCP Servers / n8n / APIs
        ↓
DGX Spark Local Models

---

# Planned Core Services

## PostgreSQL

Purpose:
- Persistent application database
- Agent state storage
- Session memory
- Workflow metadata
- Long-term memory indexing

---

## Redis

Purpose:
- Fast in-memory caching
- Queue management
- Agent communication buffer
- Session acceleration

---

## n8n

Purpose:
- Workflow automation platform
- Agent orchestration
- API automation
- Event routing

---

# DGX Spark Integration

DGX Spark is the dedicated AI inference environment.

This server communicates with DGX Spark remotely.

DGX Spark Responsibilities:
- Run local LLMs
- GPU inference
- Embeddings generation
- Multi-model routing
- High-performance AI workloads

---

## Ollama

Status:
- Not required

Reason:
- DGX Spark handles local inference

---

# MCP Server Strategy

MCP servers act as modular tool providers.

Examples:
- ServiceNow MCP
- GitHub MCP
- Filesystem MCP
- PostgreSQL MCP
- Email MCP
- Calendar MCP

---

# Networking Strategy

## Public Exposure

Only expose:
- Nginx Proxy Manager
- Cora frontend
- required APIs

Avoid exposing:
- databases
- Redis
- internal MCP servers
- internal orchestration services

---

# Security Philosophy

- Minimal exposed ports
- Reverse-proxy-first design
- Internal-only service networking
- SSL termination through NPM
- Secrets stored in environment variables
- Avoid hardcoded credentials

---

# Rules

Behavioral guidelines to reduce common LLM coding mistakes, derived from [Andrej Karpathy's observations](https://x.com/karpathy/status/2015883857489522876) on LLM coding pitfalls.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.
