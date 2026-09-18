import React, { useCallback, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../api";
import type { Document } from "../types";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  EmptyState,
  ErrorBanner,
  Spinner,
} from "../components/ui";

/**
 * F1 ingestion admin page (PRD §4 F1, §14 demo step 2).
 *
 * `GET /api/policies` returns each document with an extra `chunk_count`
 * field beyond the frozen `Document` schema (see `app/routers/policies.py`
 * -- it deliberately skips `response_model` there so that additive field
 * survives). `types.ts` is frozen and doesn't declare it, so this local
 * interface widens the shared type just for this page.
 */
interface PolicyDocument extends Document {
  chunk_count: number;
}

const ACCEPTED_EXTENSIONS = [".pdf", ".md", ".markdown", ".txt"];

type UploadStatus = "uploading" | "done" | "error";

interface UploadItem {
  key: string;
  fileName: string;
  status: UploadStatus;
  chunkCount?: number;
  errorMessage?: string;
}

// `api.ts` (frozen, owned by A0) has no DELETE-policy helper -- it wasn't
// part of the Wave-0 contract surface. Mirrors that module's own request
// conventions (X-Demo-User header, `{error:{code,message}}` envelope ->
// `ApiError`) rather than inventing a different error shape.
const DEMO_USER = "demo-user";

async function deletePolicyDocument(documentId: string): Promise<void> {
  const res = await fetch(`/api/policies/${encodeURIComponent(documentId)}`, {
    method: "DELETE",
    headers: { "X-Demo-User": DEMO_USER },
  });
  if (res.ok) return;

  const raw = await res.text();
  let code = "unknown_error";
  let message = res.statusText || `Request failed with status ${res.status}`;
  if (raw) {
    try {
      const parsed = JSON.parse(raw) as { error?: { code?: string; message?: string } };
      code = parsed.error?.code ?? code;
      message = parsed.error?.message ?? message;
    } catch {
      // Non-JSON error body -- keep the defaults above.
    }
  }
  throw new ApiError(code, message, res.status);
}

function hasAcceptedExtension(fileName: string): boolean {
  const lower = fileName.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function errorMessageOf(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

export default function Admin() {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragActive, setDragActive] = useState(false);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const policiesQuery = useQuery({
    queryKey: ["policies"],
    queryFn: () => api.listPolicies(),
  });

  const uploadFiles = useCallback(
    async (fileList: FileList | File[]) => {
      const files = Array.from(fileList);
      if (files.length === 0) return;

      for (const file of files) {
        const key = `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(36).slice(2)}`;

        if (!hasAcceptedExtension(file.name)) {
          setUploads((prev) => [
            ...prev,
            {
              key,
              fileName: file.name,
              status: "error",
              errorMessage: "Unsupported file type — upload a .pdf, .md, or .txt file.",
            },
          ]);
          continue;
        }

        setUploads((prev) => [...prev, { key, fileName: file.name, status: "uploading" }]);
        try {
          // eslint-disable-next-line no-await-in-loop -- uploads are sequential on purpose, so
          // per-file progress in the list above is meaningful and stays in file-drop order.
          const result = await api.uploadPolicy(file);
          setUploads((prev) =>
            prev.map((u) => (u.key === key ? { ...u, status: "done", chunkCount: result.chunk_count } : u)),
          );
          // eslint-disable-next-line no-await-in-loop
          await queryClient.invalidateQueries({ queryKey: ["policies"] });
        } catch (err) {
          const message = errorMessageOf(err, "Upload failed.");
          setUploads((prev) => prev.map((u) => (u.key === key ? { ...u, status: "error", errorMessage: message } : u)));
        }
      }
    },
    [queryClient],
  );

  const onDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragActive(false);
      if (event.dataTransfer.files.length > 0) {
        void uploadFiles(event.dataTransfer.files);
      }
    },
    [uploadFiles],
  );

  const onFileInputChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      if (event.target.files && event.target.files.length > 0) {
        void uploadFiles(event.target.files);
      }
      event.target.value = "";
    },
    [uploadFiles],
  );

  const handleDelete = useCallback(
    async (documentId: string) => {
      setDeleteError(null);
      setDeletingId(documentId);
      try {
        await deletePolicyDocument(documentId);
        await queryClient.invalidateQueries({ queryKey: ["policies"] });
      } catch (err) {
        setDeleteError(errorMessageOf(err, "Failed to delete document."));
      } finally {
        setDeletingId(null);
      }
    },
    [queryClient],
  );

  const documents = (policiesQuery.data?.documents ?? []) as unknown as PolicyDocument[];

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 p-6">
      <div>
        <h1 className="text-xl font-semibold">Policy Admin</h1>
        <p className="text-sm text-[hsl(var(--muted-foreground))]">
          Upload HR policy documents so employees can ask grounded questions about them in Policy Chat.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Before you upload real policies</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 text-sm text-[hsl(var(--muted-foreground))]">
          <p>
            Ingestion runs local embeddings only, with no network call. However, once a document is
            uploaded, any question an employee asks about it is sent to the configured third-party
            model provider and may be retained under that provider&rsquo;s data policies. Avoid
            uploading documents that contain personal data.
          </p>
          <p>
            For sensitive material, a fully local configuration is available — run{" "}
            <code>docker compose --profile offline up</code> to process everything on this machine,
            with nothing sent externally.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Upload a policy document</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div
            onDragOver={(event) => {
              event.preventDefault();
              setDragActive(true);
            }}
            onDragLeave={() => setDragActive(false)}
            onDrop={onDrop}
            className={`flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-8 text-center transition-colors ${
              dragActive ? "border-[hsl(var(--accent))] bg-[hsl(var(--muted))]" : "border-[hsl(var(--border))]"
            }`}
          >
            <p className="text-sm font-medium">Drag &amp; drop a PDF, Markdown, or text file here</p>
            <p className="text-xs text-[hsl(var(--muted-foreground))]">or</p>
            <Button variant="outline" size="sm" onClick={() => inputRef.current?.click()}>
              Choose file
            </Button>
            <input
              ref={inputRef}
              type="file"
              accept=".pdf,.md,.markdown,.txt"
              multiple
              className="hidden"
              onChange={onFileInputChange}
            />
          </div>

          {uploads.length > 0 && (
            <ul className="flex flex-col gap-2">
              {uploads.map((u) => (
                <li
                  key={u.key}
                  className="flex items-center justify-between gap-3 rounded-md border border-[hsl(var(--border))] px-3 py-2 text-sm"
                >
                  <span className="truncate">{u.fileName}</span>
                  {u.status === "uploading" && <Spinner size="sm" label="Uploading" />}
                  {u.status === "done" && (
                    <Badge variant="success">
                      {u.chunkCount} chunk{u.chunkCount === 1 ? "" : "s"} ingested
                    </Badge>
                  )}
                  {u.status === "error" && <Badge variant="danger">{u.errorMessage ?? "Upload failed"}</Badge>}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Ingested documents</CardTitle>
        </CardHeader>
        <CardContent>
          {deleteError && <ErrorBanner message={deleteError} className="mb-3" />}

          {policiesQuery.isLoading && (
            <div className="flex items-center justify-center p-8">
              <Spinner label="Loading documents" />
            </div>
          )}

          {policiesQuery.isError && (
            <ErrorBanner
              message={errorMessageOf(policiesQuery.error, "Failed to load documents.")}
              onRetry={() => void policiesQuery.refetch()}
            />
          )}

          {!policiesQuery.isLoading && !policiesQuery.isError && documents.length === 0 && (
            <EmptyState
              title="No policies ingested yet"
              description="Upload a policy file above to get started."
            />
          )}

          {!policiesQuery.isLoading && documents.length > 0 && (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-[hsl(var(--border))] text-xs uppercase text-[hsl(var(--muted-foreground))]">
                  <th className="py-2 font-medium">Title</th>
                  <th className="py-2 font-medium">Kind</th>
                  <th className="py-2 font-medium">Chunks</th>
                  <th className="py-2 font-medium">Uploaded</th>
                  <th className="py-2" />
                </tr>
              </thead>
              <tbody>
                {documents.map((doc) => (
                  <tr key={doc.id} className="border-b border-[hsl(var(--border))] last:border-0">
                    <td className="py-2 pr-2">{doc.title}</td>
                    <td className="py-2 pr-2">
                      <Badge variant="info">{doc.kind}</Badge>
                    </td>
                    <td className="py-2 pr-2 font-medium">{doc.chunk_count}</td>
                    <td className="py-2 pr-2 text-[hsl(var(--muted-foreground))]">{formatDate(doc.uploaded_at)}</td>
                    <td className="py-2 text-right">
                      <Button
                        variant="destructive"
                        size="sm"
                        isLoading={deletingId === doc.id}
                        onClick={() => void handleDelete(doc.id)}
                      >
                        Delete
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
