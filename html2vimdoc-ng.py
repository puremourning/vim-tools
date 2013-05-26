#!/usr/bin/env python

# Missing features:
# TODO Generate table of contents from headings.
# TODO Find tag definitions in headings and mark them.
# TODO Find tag references in text and mark them.
#
# Finding the right abstractions:
# FIXME Quirky mix of classes and functions?
# FIXME Node joining is all over the place... What if every node can indicate how it wants to be joined with other nodes?

"""
html2vimdoc
===========

The ``html2vimdoc`` module takes HTML documents and converts them to Vim help
files. It tries to produce Vim help files that are a pleasure to read while
preserving as much information as possible from the original HTML document.
Here are some of the design goals of ``html2vimdoc``:

- Flexible HTML parsing powered by ``BeautifulSoup``;
- Support for nested block level elements, e.g. nested lists;
- Automatically generates a table of contents based on headings;
- Translates hyper links into external references (which are included in an
  appendix) and rewrites hyper links that point to Vim's online documentation
  into help tags which can be followed inside Vim.

How does it work?
-----------------

The ``html2vimdoc`` module works in three phases:

1. It parses the HTML document using ``BeautifulSoup``;
2. It converts the parse tree produced by ``BeautifulSoup`` into a
   simpler format that makes it easier to convert to a Vim help file;
3. It generates a Vim help file by walking through the simplified parse tree
   using recursion.
"""

# Standard library modules.
import logging
import re
import textwrap
import urllib

# External dependency, install with:
#   sudo apt-get install python-beautifulsoup
#   pip install beautifulsoup
from BeautifulSoup import BeautifulSoup, NavigableString

# External dependency, install with:
#  pip install coloredlogs
import coloredlogs

# External dependency, bundled because it's not on PyPi.
import libs.soupselect as soupselect

# Sensible defaults (you probably shouldn't change these).
TEXT_WIDTH = 79
SHIFT_WIDTH = 2

# Initialize the logging subsystem.
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(coloredlogs.ColoredStreamHandler())

# Mapping of HTML element names to custom Node types.
name_to_type_mapping = {}

def main():
    filename = 'demo/lpeg-0.10.html'
    filename = 'demo/apr-0.17.html'
    filename = 'test.html'
    with open(filename) as handle:
        html = handle.read()
        html = re.sub(r'test coverage: \S+', '', html)
        output = html2vimdoc(html, selectors_to_ignore=['h3 a[class=anchor]'])
        print output.encode('utf-8')

def html2vimdoc(html, content_selector='#content', selectors_to_ignore=[]):
    """
    Convert HTML documents to the Vim help file format.
    """
    html = decode_hexadecimal_entities(html)
    tree = BeautifulSoup(html, convertEntities=BeautifulSoup.ALL_ENTITIES)
    ignore_given_selectors(tree, selectors_to_ignore)
    root = find_root_node(tree, content_selector)
    simple_tree = simplify_tree(root)
    shift_headings(simple_tree)
    references = find_references(simple_tree)
    vimdoc = simple_tree.render(level=0)
    return vimdoc + list_references(references) + "vim: ft=help"

def decode_hexadecimal_entities(html):
    """
    Based on my testing BeautifulSoup doesn't support hexadecimal HTML
    entities, so we have to decode them ourselves :-(
    """
    # If we happen to decode an entity into one of these characters, we
    # should never insert it literally into the HTML because we'll screw
    # up the syntax.
    unsafe_to_decode = {
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&apos;',
            '&': '&amp;',
    }
    def decode_entity(match):
        character = chr(int(match.group(1), 16))
        return unsafe_to_decode.get(character, character)
    return re.sub(r'&#x([0-9A-Fa-f]+);', decode_entity, html)

def find_root_node(tree, selector):
    """
    Given a document tree generated by BeautifulSoup, find the most
    specific document node that doesn't "lose any information" (i.e.
    everything that we want to be included in the Vim help file) while
    ignoring as much fluff as possible (e.g. headers, footers and
    navigation menus included in the original HTML document).
    """
    # Try to find the root node using a CSS selector provided by the caller.
    matches = soupselect.select(tree, selector)
    if matches:
        return matches[0]
    # Otherwise we'll fall back to the <body> element.
    try:
        return tree.html.body
    except:
        # Don't break when html.body doesn't exist.
        return tree

def ignore_given_selectors(tree, selectors_to_ignore):
    """
    Remove all HTML elements matching any of the CSS selectors provided by
    the caller from the parse tree generated by BeautifulSoup.
    """
    for selector in selectors_to_ignore:
        for element in soupselect.select(tree, selector):
            element.extract()

def simplify_tree(tree):
    """
    Simplify the tree generated by BeautifulSoup into something we can
    easily generate a Vim help file from.
    """
    return simplify_node(tree)

def simplify_node(html_node):
    """
    Recursive function to simplify parse trees generated by BeautifulSoup into
    something we can more easily convert into HTML.
    """
    # First we'll get text nodes out of the way since they're very common.
    if isinstance(html_node, NavigableString):
        text = html_node.string
        if text and not text.isspace():
            logger.debug("Mapping text node: %r", text)
        return Text(text=text)
    # Now we deal with all of the known & supported HTML elements.
    name = getattr(html_node, 'name', None)
    logger.debug("Trying to map HTML element <%s> ..", name)
    if name in name_to_type_mapping:
        mapped_type = name_to_type_mapping[name]
        logger.debug("Found a mapped type: %s", mapped_type.__name__)
        return mapped_type.parse(html_node)
    # Finally we improvise, trying not to lose information.
    logger.warn("Not a supported element, improvising ..")
    return simplify_children(html_node)

def simplify_children(node):
    """
    Simplify the child nodes of the given node taken from a parse tree
    generated by BeautifulSoup.
    """
    contents = []
    for child in getattr(node, 'contents', []):
        simplified_child = simplify_node(child)
        if simplified_child:
            contents.append(simplified_child)
    if is_block_level(contents):
        logger.debug("Sequence contains some block level elements")
        return BlockLevelSequence(contents=contents)
    else:
        logger.debug("Sequence contains only inline elements")
        return InlineSequence(contents=contents)

def shift_headings(root):
    """
    Perform an intermediate pass over the simplified parse tree to shift
    headings in such a way that top level headings have level 1.
    """
    # Find the largest headings (lowest level).
    min_level = None
    logger.debug("Finding largest headings ..")
    for node in walk_tree(root):
        if isinstance(node, Heading):
            if min_level is None:
                min_level = node.level
            elif node.level < min_level:
                min_level = node.level
    if min_level is None:
        logger.debug("HTML document doesn't contain any headings?")
        return
    else:
        logger.debug("Largest headings have level %i.", min_level)
    # Shift the headings if necessary.
    if min_level > 1:
        to_subtract = min_level - 1
        logger.debug("Shifting headings by %i levels.", to_subtract)
        for node in walk_tree(root):
            if isinstance(node, Heading):
                node.level -= to_subtract

def find_references(root):
    """
    Find all hyper links in the HTML document and give each a unique number for
    reference.
    """
    by_target = {}
    by_reference = []
    logger.debug("Finding references ..")
    for node in walk_tree(root):
        if isinstance(node, HyperLink):
            target = urllib.unquote(node.target)
            # Don't reference a given URL more than once.
            if target in by_target:
                continue
            # Exclude relative URLs and literal URLs from list of references.
            if '://' not in target or target == node.text:
                continue
            number = len(by_reference) + 1
            logger.debug("Extracting reference #%i to %s ..", number, target)
            r = Reference(number=number, target=target)
            by_reference.append(r)
            by_target[target] = r
            node.reference = r
    logger.debug("Extracted %i references.", len(by_reference))
    return by_reference

def list_references(references):
    """
    Generate an overview of references to hyper links.
    """
    lines = []
    for r in references:
        lines.append(r.render(level=0))
    logger.debug("Rendered %i references.", len(lines))
    if lines:
        heading = Heading(level=1, contents=[Text(text="References")])
        return "\n\n" + heading.render(level=0) + "\n\n" + "\n".join(lines) + "\n\n"
    return "\n\n"

def walk_tree(root):
    """
    Generator that makes it easy to walk through the simplified parse tree.
    Walks through the tree in the linear order of the nodes (reading order).
    """
    flattened = []
    def recurse(node):
        flattened.append(node)
        for child in getattr(node, 'contents', []):
            recurse(child)
    recurse(root)
    return flattened

# Decorators.

def html_element(*element_names):
    """
    Decorator to associate AST nodes and HTML nodes at the point where the AST
    node is defined.
    """
    def wrap(c):
        for name in element_names:
            name_to_type_mapping[name] = c
        return c
    return wrap

# Abstract parse tree nodes.

class Node(object):

    """
    Abstract superclass for all parse tree nodes.
    """

    def __init__(self, **kw):
        """
        Short term hack for prototyping :-).
        """
        self.__dict__ = kw

    def __iter__(self):
        """
        Short term hack to make it easy to walk the tree.
        """
        return iter(getattr(self, 'contents', []))

    def __repr__(self):
        """
        Dumb but useful representation of parse tree for debugging purposes.
        """
        children = ",\n".join(repr(c) for c in self.contents)
        return "%s(%s)" % (self.__class__.__name__, children)

    @classmethod
    def parse(cls, html_node):
        """
        Default parse behavior: Just simplify any child nodes.
        """
        return cls(contents=simplify_children(html_node))

class BlockLevelNode(Node):
    """
    Abstract superclass for all block level parse tree nodes. Block level nodes
    are the nodes which take care of indentation and line wrapping by
    themselves.
    """
    pass

class InlineNode(Node):
    """
    Abstract superclass for all inline parse tree nodes. Inline nodes are the
    nodes which are subject to indenting and line wrapping by the block level
    nodes that contain them.
    """
    pass

# Concrete parse tree nodes.

class BlockLevelSequence(BlockLevelNode):

    """
    A sequence of one or more block level nodes.
    """

    def render(self, level):
        return join_blocks(self.contents, level=level)

@html_element('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
class Heading(BlockLevelNode):

    """
    Block level node to represent headings. Maps to the HTML elements ``<h1>``
    to ``<h6>``, however Vim help files have only two levels of headings so
    during conversion some information about the structure of the original
    document is lost.
    """

    @staticmethod
    def parse(html_node):
        return Heading(level=int(html_node.name[1]),
                       contents=simplify_children(html_node))

    def render(self, level):
        # Join the inline child nodes together into a single string.
        text = join_inline(self.contents, level=level)
        # Wrap the heading's text. The two character difference is " ~", the
        # suffix used to mark Vim help file headings.
        lines = [line + " ~" for line in textwrap.wrap(text, width=TEXT_WIDTH - 2)]
        # Add a line with the marker symbol for headings, repeated on the full
        # line, at the top of the heading.
        lines.insert(0, ('=' if self.level == 1 else '-') * 79)
        return "\n".join(lines)

@html_element('p')
class Paragraph(BlockLevelNode):

    """
    Block level node to represent paragraphs of text.
    Maps to the HTML element ``<p>``.
    """

    def render(self, level):
        indent = " " * (level * SHIFT_WIDTH)
        return "\n".join(textwrap.wrap(join_inline(self.contents, level=level),
                                       width=TEXT_WIDTH,
                                       initial_indent=indent,
                                       subsequent_indent=indent))

@html_element('pre')
class PreformattedText(BlockLevelNode):

    """
    Block level node to represent preformatted text.
    Maps to the HTML element ``<pre>``.
    """

    @staticmethod
    def parse(html_node):
        text = ''.join(html_node.findAll(text=True))
        return PreformattedText(text=text)

    @property
    def contents(self):
        return [self.text]

    def render(self, level):
        indent = " " * (level + 4)
        # Remove common indentation from the original text.
        text = textwrap.dedent(self.text)
        # Remove leading/trailing empty lines.
        lines = text.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop(-1)
        # Indent the original text.
        output = []
        for line in lines:
            output.append(indent + line)
        # Add a Vim help file marker indicating the preformatted text.
        output.insert(0, ">")
        return "\n".join(output)

@html_element('ul', 'ol')
class List(BlockLevelNode):

    """
    Block level node to represent ordered and unordered lists.
    Maps to the HTML elements ``<ol>`` and ``<ul>``.
    """

    @staticmethod
    def parse(html_node):
        return List(ordered=(html_node.name=='ol'),
                    contents=simplify_children(html_node))

    def render(self, level):
        items = []
        delimiter = '\n'
        for node in self.contents:
            if isinstance(node, ListItem):
                indent = ' ' * (level * SHIFT_WIDTH)
                bullet = '%i. ' % (len(items) + 1) if self.ordered else '- '
                text = node.render(level=level + (len(bullet) / SHIFT_WIDTH))
                items.append(indent + bullet + text.lstrip())
                if '\n' in text:
                    delimiter = '\n\n'
        return delimiter.join(items)

@html_element('li')
class ListItem(BlockLevelNode):

    """
    Block level node to represent list items.
    Maps to the HTML element ``<li>``.
    """

    def render(self, level):
        # TODO ListItem is kind of a special case? We are responsible for
        # hard wrapping any direct inline children of the list item.
        return join_smart(self.contents, level=level)

@html_element('table')
class Table(BlockLevelNode):

    """
    Block level node to represent tabular data.
    Maps to the HTML element ``<table>``.
    """

    def render(self, level):
        # TODO Parse and render tabular data.
        return ''

class Reference(BlockLevelNode):

    """
    Block level node to represent a reference to a hyper link.
    """

    def render(self, level):
        return "[%i] %s" % (self.number, self.target)

class InlineSequence(InlineNode):

    """
    Inline node to represent a sequence of one or more inline nodes.
    """

    def render(self, level):
        return join_inline(self.contents, level=level)

@html_element('a')
class HyperLink(InlineNode):

    """
    Inline node to represent hyper links.
    Maps to the HTML element ``<a>``.
    """

    @staticmethod
    def parse(html_node):
        return HyperLink(text=''.join(html_node.findAll(text=True)),
                         target=html_node['href'])

    def render(self, level):
        return "%s [%i]" % (self.text, self.reference.number)

class Text(InlineNode):

    """
    Inline node to represent a sequence of text.
    """

    @property
    def contents(self):
        return [self.text]

    def render(self, level):
        return self.text

def is_block_level(contents):
    """
    Return True if any of the nodes in the given sequence is a block level
    node, False otherwise.
    """
    return any(isinstance(n, BlockLevelNode) for n in contents)

def join_smart(nodes, level):
    """
    Join a sequence of block level and/or inline nodes into a single string.
    """
    if is_block_level(nodes):
        return join_blocks(nodes, level)
    else:
        return join_inline(nodes, level)

def join_blocks(nodes, level):
    """
    Join a sequence of block level nodes into a single string.
    """
    output = ''
    for node in nodes:
        text = node.render(level=level)
        if text and not text.isspace():
            if not output:
                output = text
            elif isinstance(node, PreformattedText):
                output += '\n' + text
            else:
                output += '\n\n' + text
    return output

def join_inline(nodes, level):
    """
    Join a sequence of inline nodes into a single string.
    """
    return compact("".join(n.render(level=level) for n in nodes))

def compact(text):
    """
    Compact whitespace in a string (also trims whitespace from the sides).
    """
    return " ".join(text.split())

if __name__ == '__main__':
    main()

# vim: ft=python ts=4 sw=4 et
