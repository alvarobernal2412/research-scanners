"""LaTeX input is converted to near-rendered text before anything is compared."""
from extract import extract_bytes, tex_to_text
from textnorm import tokenize

SRC = r"""\documentclass{llncs}
\usepackage{xcolor}
\newcommand{\rev}[1]{\textcolor{blue}{#1}}
\title{A \emph{Fine} Title}
\begin{document}
\section{Intro \textcolor{violet}{(R2)}}
Agents~\cite{a1, b2} \textcolor{olive}{change code} and \rev{run tests} ``quickly'' with $O(n)$ cost, see Fig.~\ref{fig:x}.\label{p}
\begin{equation}
 x = y + \alpha
\end{equation}
\begin{tikzpicture}
 \node {Never text};
\end{tikzpicture}
M\"uller \& Co.\footnote{Some \textbf{bold} note.} % a comment
\section*{\ackname}
\begin{thebibliography}{9}
\bibitem{a1} Smith, J.: Title. 2020.
\end{thebibliography}
\end{document}
"""


def test_markup_is_removed_and_words_are_kept():
    doc = extract_bytes(SRC.encode(), "tex")
    by_no = {l.no: l for l in doc.lines}
    assert [l.no for l in doc.lines] == list(range(1, len(SRC.splitlines()) + 1))        # numbering holds
    assert by_no[1].text == "" and by_no[3].text == "" and by_no[4].text == "A Fine Title"   # preamble: title only
    assert (by_no[6].text, by_no[6].heading_level) == ("Intro (R2)", 1)
    body = by_no[7].text
    assert body.startswith("Agents \\cite{a1, b2} change code and run tests “quickly” with cost, see Fig.")
    assert "textcolor" not in body and "olive" not in body and "fig:x" not in body and "label" not in body
    assert [by_no[n].text for n in range(8, 14)] == [""] * 6                              # equation, tikzpicture
    assert by_no[14].text == "Müller & Co.Some bold note."
    assert by_no[15].text == "Acknowledgements" and by_no[15].heading_level == 1
    assert by_no[17].text.startswith("\\bibitem{a1}")                                      # reference list untouched


def test_citations_stay_in_text_but_are_not_words():
    assert tex_to_text(r"as shown~\citep[p.~3]{k1,k2} by \emph{us}") == r"as shown \citep[p.~3]{k1,k2} by us"
    doc = extract_bytes(r"Agents \cite{a1} change code.".encode(), "tex")
    assert [t.key for t in tokenize(doc.lines)] == ["agents", "change", "code."] or \
        [t.key for t in tokenize(doc.lines)] == ["agents", "change", "code"]
