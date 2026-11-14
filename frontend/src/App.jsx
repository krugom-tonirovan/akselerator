import { createElement, useEffect, useMemo, useRef } from 'react';
import appShell from './app-shell.html?raw';
import { mountLegacyApp } from './legacyApp.js';

const attributeNames = {
  class: 'className',
  'clip-path': 'clipPath',
  'stop-color': 'stopColor',
  'stop-opacity': 'stopOpacity',
  'stroke-dasharray': 'strokeDasharray',
  'stroke-linecap': 'strokeLinecap',
  'stroke-width': 'strokeWidth',
};

const booleanAttributes = new Set(['disabled', 'hidden', 'multiple', 'open', 'required']);

function toReactNode(node, key) {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent;
  if (node.nodeType !== Node.ELEMENT_NODE) return null;

  const props = { key };
  if (node.localName === 'select') {
    const selectedOption = node.querySelector('option[selected]');
    if (selectedOption) props.defaultValue = selectedOption.getAttribute('value') || selectedOption.textContent;
  }
  for (const attribute of node.attributes) {
    if (attribute.name === 'selected' && node.localName === 'option') continue;
    const name = attribute.name === 'value' && node.localName === 'input'
      ? 'defaultValue'
      : attributeNames[attribute.name] || attribute.name;
    props[name] = booleanAttributes.has(attribute.name) ? true : attribute.value;
  }

  const children = Array.from(node.childNodes, (child, index) => toReactNode(child, index));
  return createElement(node.localName, props, ...children);
}

function parseAppShell(markup) {
  const template = document.createElement('template');
  template.innerHTML = markup;
  return Array.from(template.content.childNodes, (node, index) => toReactNode(node, index));
}

export default function App() {
  const hostRef = useRef(null);
  const appNodes = useMemo(() => parseAppShell(appShell), []);

  useEffect(() => {
    if (hostRef.current) mountLegacyApp(hostRef.current);
  }, []);

  return (
    <div
      ref={hostRef}
      className="react-app-host"
    >
      {appNodes}
    </div>
  );
}
