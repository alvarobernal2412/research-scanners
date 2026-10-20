
void strbuf_commented_addf(struct strbuf *sb, const char *comment_prefix,
			   const char *fmt, ...)
{
	va_list params;
	struct strbuf buf = STRBUF_INIT;
	int incomplete_line = sb->len && sb->buf[sb->len - 1] != '\n';

	va_start(params, fmt);
	strbuf_vaddf(&buf, fmt, params);
	va_end(params);

	strbuf_add_commented_lines(sb, buf.buf, buf.len, comment_prefix);
	if (incomplete_line)
		sb->buf[--sb->len] = '\0';

	strbuf_release(&buf);
}
