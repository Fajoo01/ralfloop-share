import os
from cat.mad_hatter.decorators import plugin

RALFLOOP_BASE_URL = os.getenv("RALFLOOP_BASE_URL", "http://127.0.0.1:19090").rstrip("/")

@plugin
def settings_schema():
    return {
        "type": "object",
        "properties": {
            "ralfloop_base_url": {
                "type": "string",
                "default": RALFLOOP_BASE_URL,
                "description": "Base URL del backend OpenShell"
            },

            "planner_model_profile": {
                "type": "string",
                "default": "generalist",
                "description": "Profilo logico planner"
            },
            "coder_model_profile": {
                "type": "string",
                "default": "coder",
                "description": "Profilo logico coder"
            },
            "judge_model_profile": {
                "type": "string",
                "default": "generalist",
                "description": "Profilo logico judge"
            },

            "planner_model_name": {
                "type": "string",
                "default": "qwen2.5:7b",
                "description": "Modello Ollama planner"
            },
            "coder_model_name": {
                "type": "string",
                "default": "qwen2.5:7b",
                "description": "Modello Ollama coder"
            },
            "judge_model_name": {
                "type": "string",
                "default": "qwen2.5:7b",
                "description": "Modello Ollama judge"
            },

            "planner_rag_collection": {
                "type": "string",
                "default": "ralfloop_planner",
                "description": "Collection RAG planner"
            },
            "coder_rag_collection": {
                "type": "string",
                "default": "ralfloop_coder",
                "description": "Collection RAG coder"
            },
            "judge_rag_collection": {
                "type": "string",
                "default": "ralfloop_judge",
                "description": "Collection RAG judge"
            },

            "temp_output_dir": {
                "type": "string",
                "default": "/ralfloop_tmp",
                "description": "Directory temporanea output con TTL 24h"
            }
        }
    }
