# Customisation and training

Precision here matters, because these terms are routinely conflated in ways that
mislead people about what their computer is actually doing.

## The six distinct things

| Technique | Changes model weights? | Persists across sessions? | Cost | Status |
|---|---|---|---|---|
| **Prompting** | No | No | Free | **Built** |
| **Persistent memory** | No | Yes (as text) | Free | Phase 3 |
| **RAG / retrieval** | No | Yes (as an index) | Cheap | Phase 3 |
| **Modelfile** | No | Yes (as a new tag) | Free | Phase 3 |
| **LoRA / QLoRA** | Yes (an adapter) | Yes | Hours, needs a GPU | Phase 5, optional |
| **Full fine-tuning** | Yes (all weights) | Yes | Days, needs serious hardware | Not planned |

### Prompting

Text you put in the context window: the system prompt, your messages, tool results.
Influences this conversation only. Costs nothing and changes nothing permanently.

`/system`, model profiles, project instructions.

### Persistent memory

Facts saved to a database and re-inserted into future prompts. It **looks** like the
model remembering, but the model is unchanged — you are re-sending the text every time.

Phase 3. It will be opt-in, inspectable and deletable. localai will never create hidden
memory.

### Retrieval-augmented generation

Index your documents; at query time find relevant chunks and paste them into the
prompt. The model is unchanged; it just has better material in front of it.

For "answer questions about my files", **this is almost always what you want** — not
fine-tuning. It is cheap, updates instantly when a document changes, and you can see
exactly what was retrieved.

Phase 3.

### Modelfiles

Ollama's packaging format. Bundles a base model with a system prompt and parameters
under a new tag:

```
FROM qwen3:8b
SYSTEM "You are a careful assistant for reviewing legal documents."
PARAMETER temperature 0.3
PARAMETER num_ctx 16384
```

```powershell
ollama create legal-review -f Modelfile
```

**This does not train anything.** It saves a configuration. The weights are identical
to `qwen3:8b`. It is a convenience, and a genuinely useful one.

Phase 3 will add creating and editing these from the interface.

### LoRA / QLoRA adapters

Genuine training. Freezes the base weights and trains a small number of new parameters
that adjust behaviour. Requires a prepared dataset, a GPU, and hours.

Good for: consistent output format, a domain vocabulary, a specific style.
Bad for: teaching new facts — use retrieval for that.

Phase 5, as an **optional module** that will detect compatible hardware, estimate memory
requirements, require explicit confirmation, log its configuration and outputs, and
support interruption and resumption where the underlying tool does.

### Full fine-tuning

Updating every weight. Days of compute on hardware most people do not have, and it
risks catastrophic forgetting. Not planned; you would use dedicated tooling.

## The thing to be clear about

**Chatting with a model never changes its weights.** Not in this application, not in any
other. When a conversation appears to "remember" something, either it is still in the
context window, or software is re-inserting stored text. The model file on disk is
byte-for-byte identical before and after.

Any product implying otherwise is describing prompting or retrieval, whatever it calls
it.

## What to actually do

| Goal | Use |
|---|---|
| Answer questions about my documents | **Retrieval** (Phase 3) |
| Always respond in a particular style | **System prompt** or a Modelfile |
| Remember my preferences between sessions | **Persistent memory** (Phase 3) |
| Consistent structured output | System prompt first; **LoRA** if that is not enough |
| Domain vocabulary | Retrieval first; **LoRA** if genuinely needed |
| Teach it new facts | **Retrieval.** Not fine-tuning. |

The ordering is deliberate: try prompting, then retrieval, then adapters. Most goals
people reach for fine-tuning to solve are solved better and far more cheaply by the
first two.

## Datasets (Phase 5)

If adapter training arrives, dataset tooling comes with it: import JSONL, validate
format, detect duplicates and near-duplicates, report length distribution, flag
problems, and export for external tools. Validation before training is the difference
between a useful adapter and hours of wasted compute.
