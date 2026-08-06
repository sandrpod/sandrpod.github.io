# blog.sandrpod.com

The SandrPod project site. Hand-written static HTML — no generator framework,
no build step at serve time, no external requests.

```
src/*.md          article source
build.py          regenerates the site from src/ (needs pandoc)
style.css         the whole design system
```

Adding a post: drop the markdown in `src/`, add an entry to `POSTS` in
`build.py`, run `python3 build.py`, commit the output. The `h1` and the italic
line under it become the title and standfirst.

Served by GitHub Pages from `main`. TLS is GitHub's, renewed automatically.
