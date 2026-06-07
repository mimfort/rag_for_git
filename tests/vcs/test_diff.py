from reviewer.vcs.diff import commentable_lines

PATCH = """@@ -1,3 +1,4 @@
 def f():
-    return 1
+    return 2
+    # new
 x = 0
"""

def test_commentable_lines_right_and_left():
    cl = commentable_lines(PATCH)
    assert cl["RIGHT"] == {1, 2, 3, 4}
    assert cl["LEFT"] == {2}
