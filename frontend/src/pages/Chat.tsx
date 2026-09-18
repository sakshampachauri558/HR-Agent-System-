/**
 * Policy RAG Chat — PRD §4 F1 / Journey A.
 *
 * `/chat` → type question → answer renders with clickable citation chips →
 * click a chip → right drawer opens the exact source chunk, highlighted,
 * with document title and section. An out-of-corpus question refuses
 * cleanly, rendered as a neutral "not covered" state rather than an error.
 *
 * Owned by A4 (RAG Query). Talks only to `POST /api/chat` via `api.chat`.
 */
import React, { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { api, ApiError } from "../api";
import type { Citation, Turn } from "../types";
import { Badge, Button, Card, CardContent, EmptyState, ErrorBanner, Spinner } from "../components/ui";

// ---------------------------------------------------------------------------
// Local view model
// ---------------------------------------------------------------------------

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  grounded?: boolean;
  error?: { code: string; message: string };
}

const EXAMPLE_QUESTIONS = [
  "How many casual leaves carry forward?",
  "What's the WFH policy for contractors?",
];

function uid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const CITATION_MARKER_SPLIT_RE = /(\[\^[0-9a-fA-F-]+\])/g;
const CITATION_MARKER_TEST_RE = /^\[\^[0-9a-fA-F-]+\]$/;

// ---------------------------------------------------------------------------
// Citation drawer
// ---------------------------------------------------------------------------

function CitationDrawer({ citation, onClose }: { citation: Citation; onClose: () => void }) {
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-black/30"
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        role="dialog"
        aria-label={`Source: ${citation.document_title}`}
        className="fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col border-l border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-xl"
      >
        <div className="flex items-start justify-between gap-3 border-b border-[hsl(var(--border))] p-4">
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-[hsl(var(--foreground))]">
              {citation.document_title}
            </p>
            <p className="mt-0.5 text-xs text-[hsl(var(--muted-foreground))]">{citation.section}</p>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close source drawer">
            Close
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">
          <p className="mb-3 text-xs text-[hsl(var(--muted-foreground))]">
            Characters {citation.start_char}–{citation.end_char} of the source document
          </p>
          <mark className="block rounded-md border-l-4 border-[hsl(var(--warning))] bg-[hsl(var(--muted))] p-3 text-sm leading-relaxed text-[hsl(var(--foreground))]">
            {citation.quote}
          </mark>
        </div>
      </aside>
    </>
  );
}

// ---------------------------------------------------------------------------
// Answer rendering — inline [^chunk_id] markers become numbered, clickable
// footnote chips positionally mapped onto the resolved `citations` array
// (only ever real, DB-resolved citations reach this component).
// ---------------------------------------------------------------------------

function AnswerBody({
  content,
  citations,
  onSelectCitation,
}: {
  content: string;
  citations: Citation[];
  onSelectCitation: (c: Citation) => void;
}) {
  const parts = content.split(CITATION_MARKER_SPLIT_RE);
  let citationIndex = 0;

  return (
    <p className="whitespace-pre-wrap text-sm leading-relaxed text-[hsl(var(--foreground))]">
      {parts.map((part, i) => {
        if (CITATION_MARKER_TEST_RE.test(part)) {
          const citation = citations[citationIndex];
          citationIndex += 1;
          if (!citation) return null; // no resolvable citation left to back this marker
          const n = citationIndex;
          return (
            <button
              key={`cite-${i}`}
              type="button"
              onClick={() => onSelectCitation(citation)}
              className="mx-0.5 inline-block align-super"
              aria-label={`View source ${n}: ${citation.document_title}, ${citation.section}`}
              title={`${citation.document_title} — ${citation.section}`}
            >
              <Badge variant="info" className="cursor-pointer px-1.5 py-0 text-[10px]">
                {n}
              </Badge>
            </button>
          );
        }
        return <React.Fragment key={`text-${i}`}>{part}</React.Fragment>;
      })}
    </p>
  );
}

function SourceChips({
  citations,
  onSelectCitation,
}: {
  citations: Citation[];
  onSelectCitation: (c: Citation) => void;
}) {
  if (citations.length === 0) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-1.5 border-t border-[hsl(var(--border))] pt-2">
      {citations.map((c, i) => (
        <button
          key={c.chunk_id}
          type="button"
          onClick={() => onSelectCitation(c)}
          className="rounded-full transition-opacity hover:opacity-80"
        >
          <Badge variant="neutral" className="cursor-pointer">
            {i + 1}. {c.document_title} · {c.section}
          </Badge>
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Message bubbles
// ---------------------------------------------------------------------------

function UserBubble({ content }: { content: string }) {
  return (
    <div className="ml-auto max-w-xl rounded-lg bg-[hsl(var(--accent))] px-4 py-2 text-sm text-[hsl(var(--accent-foreground))]">
      {content}
    </div>
  );
}

function RefusalBubble({ content }: { content: string }) {
  // Deliberately calm and neutral, not an error banner -- this is the
  // "clean refusal" moment the demo script (PRD §14 step 4) is built
  // around, and it should read as the product working correctly.
  return (
    <Card className="max-w-2xl border-[hsl(var(--border))]">
      <CardContent className="flex flex-col gap-2">
        <Badge variant="neutral" className="w-fit">
          Not covered in policy
        </Badge>
        <p className="text-sm leading-relaxed text-[hsl(var(--foreground))]">{content}</p>
      </CardContent>
    </Card>
  );
}

function GroundedBubble({
  content,
  citations,
  onSelectCitation,
}: {
  content: string;
  citations: Citation[];
  onSelectCitation: (c: Citation) => void;
}) {
  return (
    <Card className="max-w-2xl">
      <CardContent className="flex flex-col gap-1">
        <AnswerBody content={content} citations={citations} onSelectCitation={onSelectCitation} />
        <SourceChips citations={citations} onSelectCitation={onSelectCitation} />
      </CardContent>
    </Card>
  );
}

function AssistantMessage({
  message,
  onSelectCitation,
}: {
  message: ChatMessage;
  onSelectCitation: (c: Citation) => void;
}) {
  if (message.error) {
    return (
      <div className="max-w-2xl">
        <ErrorBanner code={message.error.code} message={message.error.message} />
      </div>
    );
  }
  if (!message.grounded) {
    return <RefusalBubble content={message.content} />;
  }
  return (
    <GroundedBubble
      content={message.content}
      citations={message.citations ?? []}
      onSelectCitation={onSelectCitation}
    />
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function Chat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length]);

  const mutation = useMutation({
    mutationFn: (vars: { question: string; history: Turn[] }) => api.chat(vars),
  });

  function ask(question: string) {
    const trimmed = question.trim();
    if (!trimmed || mutation.isPending) return;

    const history: Turn[] = messages.map((m) => ({ role: m.role, content: m.content }));
    setMessages((prev) => [...prev, { id: uid(), role: "user", content: trimmed }]);
    setInput("");

    mutation.mutate(
      { question: trimmed, history },
      {
        onSuccess: (resp) => {
          setMessages((prev) => [
            ...prev,
            {
              id: uid(),
              role: "assistant",
              content: resp.answer,
              citations: resp.citations,
              grounded: resp.grounded,
            },
          ]);
        },
        onError: (err) => {
          const apiErr = err instanceof ApiError ? err : null;
          setMessages((prev) => [
            ...prev,
            {
              id: uid(),
              role: "assistant",
              content: apiErr?.message ?? "Something went wrong asking that question.",
              error: {
                code: apiErr?.code ?? "unknown_error",
                message: apiErr?.message ?? "Something went wrong asking that question.",
              },
            },
          ]);
        },
      }
    );
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    ask(input);
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-[hsl(var(--border))] p-4">
        <h1 className="text-base font-semibold text-[hsl(var(--foreground))]">Policy Chat</h1>
        <p className="text-sm text-[hsl(var(--muted-foreground))]">
          Ask about HR policy. Answers are grounded in the policy corpus with citations back to
          the source clause — if it isn't written down, this assistant says so instead of
          guessing.
        </p>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        {messages.length === 0 && !mutation.isPending && (
          <EmptyState
            title="Ask your first policy question"
            description="Answers cite the exact policy clause they came from. Try one of these:"
            action={
              <div className="flex flex-wrap justify-center gap-2">
                {EXAMPLE_QUESTIONS.map((q) => (
                  <Button key={q} variant="outline" size="sm" onClick={() => ask(q)}>
                    {q}
                  </Button>
                ))}
              </div>
            }
          />
        )}

        {messages.map((m) =>
          m.role === "user" ? (
            <UserBubble key={m.id} content={m.content} />
          ) : (
            <AssistantMessage key={m.id} message={m} onSelectCitation={setSelectedCitation} />
          )
        )}

        {mutation.isPending && (
          <div className="flex max-w-2xl items-center gap-2 text-sm text-[hsl(var(--muted-foreground))]">
            <Spinner size="sm" label="Thinking" />
            Looking through the policy corpus…
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      <form onSubmit={handleSubmit} className="flex gap-2 border-t border-[hsl(var(--border))] p-4">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about HR policy…"
          disabled={mutation.isPending}
          className="flex-1 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-3 py-2 text-sm text-[hsl(var(--foreground))] outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--accent))]"
        />
        <Button type="submit" isLoading={mutation.isPending} disabled={!input.trim()}>
          Ask
        </Button>
      </form>

      {selectedCitation && (
        <CitationDrawer citation={selectedCitation} onClose={() => setSelectedCitation(null)} />
      )}
    </div>
  );
}
