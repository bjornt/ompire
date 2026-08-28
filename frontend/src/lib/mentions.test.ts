import { describe, expect, it } from "vitest";
import { activeMentionAt, insertMention } from "./mentions";

describe("activeMentionAt", () => {
  it("opens on @ at the start of the prompt", () => {
    expect(activeMentionAt("@src", 4)).toEqual({ at: 0, query: "src" });
  });

  it("opens on @ after whitespace, including a newline", () => {
    expect(activeMentionAt("fix\n@src/a", 10)).toEqual({ at: 4, query: "src/a" });
  });

  it("does not open inside a word, so an email address is left alone", () => {
    expect(activeMentionAt("someone@example.com", 19)).toBeNull();
  });

  it("closes once the token is finished by whitespace", () => {
    expect(activeMentionAt("@src/a.ts done", 14)).toBeNull();
  });

  it("reads the mention under the caret, not the last one in the prompt", () => {
    const value = "@first.ts and @second.ts";
    expect(activeMentionAt(value, 9)).toEqual({ at: 0, query: "first.ts" });
    expect(activeMentionAt(value, 24)).toEqual({ at: 14, query: "second.ts" });
  });

  it("treats a bare @ as an open mention with an empty query", () => {
    expect(activeMentionAt("look at @", 9)).toEqual({ at: 8, query: "" });
  });
});

describe("insertMention", () => {
  it("replaces the partial token and appends a single space", () => {
    const mention = activeMentionAt("read @src", 9)!;
    expect(insertMention("read @src", mention, 9, "src/lib/token.ts")).toEqual({
      value: "read @src/lib/token.ts ",
      caret: 23,
    });
  });

  it("inserts at the caret rather than appending to the end", () => {
    const value = "read @src and then stop";
    const mention = activeMentionAt(value, 9)!;

    expect(insertMention(value, mention, 9, "a/b.ts")).toEqual({
      value: "read @a/b.ts and then stop",
      caret: 12,
    });
  });

  it("does not leave a double space when the text already continues", () => {
    const value = "read @src\nnext line";
    const mention = activeMentionAt(value, 9)!;

    expect(insertMention(value, mention, 9, "a.ts").value).toEqual("read @a.ts\nnext line");
  });

  it("leaves an earlier mention untouched when a later one is completed", () => {
    const value = "@kept.ts and @par";
    const mention = activeMentionAt(value, 17)!;

    expect(insertMention(value, mention, 17, "parser.ts").value).toEqual(
      "@kept.ts and @parser.ts ",
    );
  });
});
