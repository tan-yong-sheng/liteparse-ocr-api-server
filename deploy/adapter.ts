import { LiteParse } from "../liteparse/src/lib.js";
import fs from "fs/promises";
import axios from "axios";
import path from "path";
import os from "os";
import { execFile } from "child_process";
import { promisify } from "util";

const execFileAsync = promisify(execFile);
const MAX_PAGES_PER_CHUNK = 1000;
const OCR_NUM_WORKERS = 6;
const FILES_ROOT = "/root/mistral-files";
const TESSERACT_MODEL_NAMES = new Set([
  "liteparse-tesseract-latest",
]);
const PADDLEOCR_MODEL_NAMES = new Set([
  "liteparse-paddleocr-latest",
]);
const DEFAULT_MODEL = "liteparse-tesseract-latest";
const PADDLEOCR_SERVER_URL = "http://127.0.0.1:8829/ocr";

type OcrPage = {
  pageNum: number;
  text: string;
  height: number;
  width: number;
};

type OcrResult = {
  pages: OcrPage[];
};

type RequestBody = {
  model?: string;
  pages?: number[] | null;
  document?: {
    document_url?: string;
    image_url?: string;
    file_content?: string;
    file_id?: string;
    fileId?: string;
  };
};

type OcrBackend = "tesseract" | "paddleocr";

async function downloadFile(url: string): Promise<Buffer> {
  if (url.startsWith("data:")) {
    const match = url.match(/^data:([^;,]+)?(;base64)?,(.*)$/s);
    if (!match) {
      throw new Error("Invalid data URL");
    }

    const isBase64 = Boolean(match[2]);
    const payload = match[3] ?? "";
    return isBase64
      ? Buffer.from(payload, "base64")
      : Buffer.from(decodeURIComponent(payload), "utf8");
  }

    const response = await axios.get(url, { responseType: 'arraybuffer' });
    return Buffer.from(response.data);
}

async function loadStoredFile(fileId: string): Promise<Buffer> {
  const normalizedId = fileId.trim();
  if (!/^[0-9a-fA-F-]{36}$/.test(normalizedId)) {
    throw new Error(`Invalid file_id: ${fileId}`);
  }

  const metaPath = path.join(FILES_ROOT, normalizedId, "meta.json");
  const contentPath = path.join(FILES_ROOT, normalizedId, "content");
  const metaRaw = await fs.readFile(metaPath, "utf8");
  const meta = JSON.parse(metaRaw);
  if (meta.deleted) {
    throw new Error(`File ${fileId} is deleted`);
  }
  if (typeof meta.expiresAt === "number" && meta.expiresAt <= Math.floor(Date.now() / 1000)) {
    throw new Error(`File ${fileId} is expired`);
  }
  return await fs.readFile(contentPath);
}

async function readStdin(): Promise<string> {
  return await new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", chunk => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

function normalizeModelName(model: string | undefined | null): string {
  return (model || "").trim().toLowerCase();
}

function resolveModel(model: string | undefined | null): { model: string; backend: OcrBackend } {
  const normalized = normalizeModelName(model);

  if (PADDLEOCR_MODEL_NAMES.has(normalized)) {
    return { model: "liteparse-paddleocr-latest", backend: "paddleocr" };
  }

  if (TESSERACT_MODEL_NAMES.has(normalized) || normalized === "") {
    return { model: DEFAULT_MODEL, backend: "tesseract" };
  }

  console.warn(`Unknown model '${model}', defaulting to ${DEFAULT_MODEL}`);
  return { model: DEFAULT_MODEL, backend: "tesseract" };
}

function normalizePages(pages: number[] | null | undefined): number[] | undefined {
  if (!Array.isArray(pages) || pages.length === 0) {
    return undefined;
  }

  return [...new Set(pages)]
    .filter((page) => Number.isInteger(page) && page >= 0)
    .sort((a, b) => a - b);
}

function pagesToTargetPages(pages: number[]): string {
  const ranges: string[] = [];
  let start = pages[0];
  let end = pages[0];

  for (let i = 1; i < pages.length; i += 1) {
    const page = pages[i];
    if (page === end + 1) {
      end = page;
      continue;
    }

    ranges.push(start === end ? `${start + 1}` : `${start + 1}-${end + 1}`);
    start = page;
    end = page;
  }

  ranges.push(start === end ? `${start + 1}` : `${start + 1}-${end + 1}`);
  return ranges.join(",");
}

function buildChunkRanges(totalPages: number, chunkSize: number): string[] {
  const ranges: string[] = [];

  for (let start = 1; start <= totalPages; start += chunkSize) {
    const end = Math.min(totalPages, start + chunkSize - 1);
    ranges.push(start === end ? `${start}` : `${start}-${end}`);
  }

  return ranges;
}

async function getPdfPageCount(input: Buffer): Promise<number | null> {
  const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "liteparse-ocr-"));
  const tempFile = path.join(tempDir, "document.pdf");

  try {
    await fs.writeFile(tempFile, input);
    const { stdout } = await execFileAsync("pdfinfo", [tempFile], {
      encoding: "utf8",
      maxBuffer: 1024 * 1024,
    });
    const match = stdout.match(/^Pages:\s+(\d+)$/m);
    return match ? parseInt(match[1], 10) : null;
  } catch {
    return null;
  } finally {
    await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {});
  }
}

function createParser(
  targetPages?: string,
  maxPages = MAX_PAGES_PER_CHUNK,
  backend: OcrBackend = "tesseract",
) {
  const ocrLanguage = backend === "paddleocr" ? "en" : "eng";
  return new LiteParse({
    ocrEnabled: true,
    outputFormat: "json",
    ocrLanguage: ocrLanguage,
    ocrServerUrl: backend === "paddleocr" ? PADDLEOCR_SERVER_URL : undefined,
    numWorkers: OCR_NUM_WORKERS,
    targetPages: targetPages,
    maxPages: maxPages,
  });
}

function toMistralPages(pages: OcrPage[], dpi: number) {
  return pages.map((p) => {
    return {
      index: p.pageNum - 1,
      markdown: p.text,
      dimensions: {
        dpi: dpi,
        height: Math.round(p.height),
        width: Math.round(p.width),
      },
      images: [],
    };
  });
}

async function main() {
  const requestBodyStr = process.argv[2] || await readStdin();
  if (!requestBodyStr) {
    process.stderr.write("No request body provided\n");
    process.exit(1);
  }

  const requestBody = JSON.parse(requestBodyStr) as RequestBody;
  const resolvedModel = resolveModel(requestBody.model);

  let input: any;
  const doc = requestBody.document;
  if (doc) {
    const url = doc.document_url ?? doc.image_url;
    if (url) {
      try {
        input = await downloadFile(url);
      } catch (err: any) {
        process.stderr.write(JSON.stringify({ error: `Failed to download file from URL: ${err.message}` }));
        process.exit(1);
      }
    } else if (doc.file_content) {
      input = Buffer.from(doc.file_content, "base64");
    } else if (doc.file_id) {
      try {
        input = await loadStoredFile(doc.file_id);
      } catch (err: any) {
        process.stderr.write(JSON.stringify({ error: `Failed to load file_id ${doc.file_id}: ${err.message}` }));
        process.exit(1);
      }
    } else if ((doc as any).fileId) {
      try {
        input = await loadStoredFile((doc as any).fileId);
      } catch (err: any) {
        process.stderr.write(JSON.stringify({ error: `Failed to load fileId ${(doc as any).fileId}: ${err.message}` }));
        process.exit(1);
      }
    }
  }

  if (!input) {
    process.stderr.write(JSON.stringify({ error: "Missing document source (document_url or file_content)" }));
    process.exit(1);
  }

  try {
    const explicitPages = normalizePages(requestBody.pages);
    const pageCount = Buffer.isBuffer(input) ? await getPdfPageCount(input) : null;

    const chunkTargets: { targetPages?: string; maxPages?: number }[] = [];
    if (explicitPages && explicitPages.length > MAX_PAGES_PER_CHUNK) {
      for (let i = 0; i < explicitPages.length; i += MAX_PAGES_PER_CHUNK) {
        const chunk = explicitPages.slice(i, i + MAX_PAGES_PER_CHUNK);
        chunkTargets.push({
          targetPages: pagesToTargetPages(chunk),
          maxPages: chunk.length,
        });
      }
    } else if (explicitPages && explicitPages.length > 0) {
      chunkTargets.push({
        targetPages: pagesToTargetPages(explicitPages),
        maxPages: explicitPages.length,
      });
    } else if (pageCount && pageCount > MAX_PAGES_PER_CHUNK) {
      for (const targetPages of buildChunkRanges(pageCount, MAX_PAGES_PER_CHUNK)) {
        chunkTargets.push({
          targetPages,
          maxPages: MAX_PAGES_PER_CHUNK,
        });
      }
    } else {
      chunkTargets.push({});
    }

    const allPages: OcrPage[] = [];
    let dpi = 150;

    for (const chunk of chunkTargets) {
      const parser = createParser(chunk.targetPages, chunk.maxPages, resolvedModel.backend);
      dpi = parser.getConfig().dpi;
      const parseInput = Buffer.isBuffer(input) ? Buffer.from(input) : input;
      const result = await parser.parse(parseInput, true) as OcrResult;
      allPages.push(...result.pages);
    }

    allPages.sort((a, b) => a.pageNum - b.pageNum);
    const pages = toMistralPages(allPages, dpi);

    const response = {
      pages: pages,
      model: resolvedModel.model,
      usage_info: { 
        pages_processed: pages.length,
        total_tokens: 0 
      }
    };

    process.stdout.write(JSON.stringify(response));
  } catch (err: any) {
    process.stderr.write(JSON.stringify({ error: err.message, stack: err.stack }));
    process.exit(1);
  }
}

main().catch(err => {
    process.stderr.write(JSON.stringify({ error: err.message, stack: err.stack }));
    process.exit(1);
});
