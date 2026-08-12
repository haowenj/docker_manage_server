import test from "node:test";
import assert from "node:assert/strict";
import {
  imageLabel,
  imagesListUrl,
  selectionState,
} from "../../src/docker_manage_server/static/js/image_batch_delete.mjs";

test("selectionState counts selection and derives all/partial states", () => {
  assert.deepEqual(selectionState([false, false]), {
    count: 0,
    canDelete: false,
    all: false,
    indeterminate: false,
  });
  assert.deepEqual(selectionState([true, false]), {
    count: 1,
    canDelete: true,
    all: false,
    indeterminate: true,
  });
  assert.deepEqual(selectionState([true, true]), {
    count: 2,
    canDelete: true,
    all: true,
    indeterminate: false,
  });
});

test("imageLabel prefers a tag and identifies dangling images", () => {
  assert.equal(
    imageLabel({ id: "sha256:a", short_id: "a", tags: ["demo:1"] }),
    "demo:1",
  );
  assert.equal(
    imageLabel({ id: "sha256:b", short_id: "b", tags: [] }),
    "b（未标记）",
  );
});

test("imagesListUrl preserves unicode query and suggested page", () => {
  assert.equal(
    imagesListUrl("演示 app", 2),
    "/images?q=%E6%BC%94%E7%A4%BA+app&page=2",
  );
  assert.equal(imagesListUrl("", 1), "/images?page=1");
});
