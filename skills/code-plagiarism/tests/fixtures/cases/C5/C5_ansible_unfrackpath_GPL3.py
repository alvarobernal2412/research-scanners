def unfrackpath(path: str, follow: bool = True, basedir: str | None = None) -> str:
    """
    Returns a path that is free of symlinks (if follow=True), environment variables, relative path traversals and symbols (~)

    :arg path: A byte or text string representing a path to be canonicalized
    :arg follow: A boolean to indicate of symlinks should be resolved or not
    :arg basedir: A byte string, text string, PathLike object, or `None`
        representing where a relative path should be resolved from.
        `None` will be substituted for the current working directory.
    :raises UnicodeDecodeError: If the canonicalized version of the path
        contains non-utf8 byte sequences.
    :rtype: A text string (unicode on pyyhon2, str on python3).
    :returns: An absolute path with symlinks, environment variables, and tilde
        expanded.  Note that this does not check whether a path exists.

    example::
        '$HOME/../../var/mail' becomes '/var/spool/mail'
    """

    if basedir is None:
        basedir = os.getcwd()
    elif os.path.isfile(basedir):
        basedir = os.path.dirname(basedir)

    final_path = os.path.expanduser(os.path.expandvars(path))

    if not os.path.isabs(final_path):
        final_path = os.path.join(basedir, final_path)

    if follow:
        final_path = os.path.realpath(final_path)

    return os.path.normpath(final_path)

