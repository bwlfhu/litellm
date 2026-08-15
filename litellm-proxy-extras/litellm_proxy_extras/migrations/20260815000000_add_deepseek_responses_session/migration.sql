CREATE TABLE "LiteLLM_DeepSeekResponsesSession" (
    "response_id" TEXT NOT NULL,
    "owner_id" TEXT NOT NULL,
    "encrypted_payload" JSONB NOT NULL,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "LiteLLM_DeepSeekResponsesSession_pkey" PRIMARY KEY ("response_id")
);

CREATE INDEX "LiteLLM_DeepSeekResponsesSession_owner_id_idx"
ON "LiteLLM_DeepSeekResponsesSession"("owner_id");
